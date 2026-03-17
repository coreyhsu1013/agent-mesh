"""
Claude Account Pool — least-loaded CLAUDE_CONFIG_DIR for multi-account rate limit distribution.

Usage:
  pool = ClaudeAccountPool(["~/.claude", "~/.claude-b", "~/.claude-c"])
  env = await pool.next_env(model="opus")  # returns env dict with CLAUDE_CONFIG_DIR set

Query:
  python -m src.auth.claude_account_pool          # show today's usage + real token stats
  python -m src.auth.claude_account_pool --reset   # reset session counters

Load balancing reads each account's stats-cache.json (real weekly token usage from Claude CLI)
plus session-level call tracking for fine-grained distribution.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Model weights: heavier models consume more rate limit
MODEL_WEIGHTS = {
    "opus": 3.0,
    "sonnet": 1.0,
}
DEFAULT_WEIGHT = 1.0

# Balance threshold: ignore token differences within this % (accounts considered balanced)
BALANCE_THRESHOLD = 0.15
# Bias scale: how aggressively to penalize higher-usage accounts (weight units)
BALANCE_BIAS_SCALE = 20.0

USAGE_FILE = os.path.expanduser("~/.agent-mesh/account-usage.json")

# Module-level singleton
_pool: ClaudeAccountPool | None = None


def _read_stats_cache(config_dir: str) -> dict:
    """
    Read Claude CLI's stats-cache.json for an account.
    Returns cumulative modelUsage + recent daily tokens.
    """
    stats_file = os.path.join(config_dir, "stats-cache.json")
    if not os.path.exists(stats_file):
        return {}
    try:
        with open(stats_file) as f:
            data = json.load(f)

        # Recent daily tokens (last 7 days, may be stale if cache not refreshed)
        today = date.today()
        week_start = today - timedelta(days=6)

        recent_tokens = 0
        recent_by_model: dict[str, int] = {}

        for entry in data.get("dailyModelTokens", []):
            entry_date = entry.get("date", "")
            try:
                d = date.fromisoformat(entry_date)
            except ValueError:
                continue
            if d < week_start:
                continue
            for model, tokens in entry.get("tokensByModel", {}).items():
                recent_tokens += tokens
                recent_by_model[model] = recent_by_model.get(model, 0) + tokens

        # Cumulative model usage (more reliable, always up to date)
        model_usage = data.get("modelUsage", {})
        cumulative_by_model: dict[str, dict] = {}
        total_output = 0
        for model, usage in model_usage.items():
            inp = usage.get("inputTokens", 0)
            out = usage.get("outputTokens", 0)
            cache_read = usage.get("cacheReadInputTokens", 0)
            total_output += out
            cumulative_by_model[model] = {
                "input": inp,
                "output": out,
                "cache_read": cache_read,
            }

        return {
            "recent_tokens": recent_tokens,
            "recent_by_model": recent_by_model,
            "cumulative_by_model": cumulative_by_model,
            "total_output": total_output,
            "total_messages": data.get("totalMessages", 0),
            "total_sessions": data.get("totalSessions", 0),
            "last_computed": data.get("lastComputedDate", ""),
        }
    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.debug(f"[AccountPool] failed to read stats-cache from {config_dir}: {e}")
        return {}


class ClaudeAccountPool:
    """Least-loaded pool of Claude config directories."""

    def __init__(self, config_dirs: list[str | dict]):
        self._accounts: list[dict] = []
        self._lock = asyncio.Lock()

        if config_dirs:
            for entry in config_dirs:
                # Support both string and dict format:
                #   "~/.claude"  OR  {"path": "~/.claude", "initial_usage": 100}
                if isinstance(entry, dict):
                    d = entry.get("path", "")
                    initial = float(entry.get("initial_usage", 0))
                else:
                    d = entry
                    initial = 0.0
                self._accounts.append({
                    "dir": os.path.expanduser(d),
                    "usage": initial,
                    "initial_usage": initial,
                    "calls": 0,
                    "calls_opus": 0,
                    "calls_sonnet": 0,
                    "calls_other": 0,
                })
            self._load_usage()
            self._apply_real_token_bias()
            dirs_info = [
                f"{a['dir']}(+{a['initial_usage']:.0f})" if a.get("initial_usage", 0) > 0
                else a["dir"]
                for a in self._accounts
            ]
            logger.info(f"[AccountPool] {len(self._accounts)} Claude accounts: {dirs_info}")

    def _apply_real_token_bias(self) -> None:
        """
        Read real token usage from stats-cache.json and add bias to usage scores.
        Accounts that already used more tokens this week get higher initial usage,
        so least-loaded picks the fresher account.
        Only applies on first load (startup bias).
        Ignores differences within 15% (BALANCE_THRESHOLD) — considered balanced.
        """
        if len(self._accounts) <= 1:
            return

        week_tokens_per_account = []
        for account in self._accounts:
            stats = _read_stats_cache(account["dir"])
            # Use total_output as proxy — accounts with more output used more capacity
            wt = stats.get("total_output", 0)
            week_tokens_per_account.append(wt)

        # If all zeros (no stats), skip
        if max(week_tokens_per_account) == 0:
            return

        min_tokens = min(week_tokens_per_account)
        max_tokens = max(week_tokens_per_account)

        if max_tokens == 0:
            return

        # Skip if difference is within threshold — accounts are balanced enough
        diff_pct = (max_tokens - min_tokens) / max_tokens
        if diff_pct <= BALANCE_THRESHOLD:
            logger.info(
                f"[AccountPool] token diff {diff_pct:.0%} <= {BALANCE_THRESHOLD:.0%} threshold, "
                f"no bias applied"
            )
            return

        # Bias proportional to excess over the balanced range
        # Higher-usage accounts get penalized so least-loaded picks the fresher one
        for i, account in enumerate(self._accounts):
            excess = (week_tokens_per_account[i] - min_tokens) / max_tokens
            bias = excess * BALANCE_BIAS_SCALE
            account["usage"] += bias
            if bias > 0:
                logger.info(
                    f"[AccountPool] {account['dir']}: week_tokens={week_tokens_per_account[i]:,}, "
                    f"diff={excess:.0%}, bias=+{bias:.1f}"
                )

    async def next_env(self, model: str = "") -> dict[str, str]:
        """Return env dict for least-used account. Empty dict = use default."""
        if not self._accounts:
            return {}

        weight = self._get_weight(model)
        model_lower = model.lower()

        async with self._lock:
            # Pick account with minimum usage
            account = min(self._accounts, key=lambda a: a["usage"])
            account["usage"] += weight
            account["calls"] += 1
            if "opus" in model_lower:
                account["calls_opus"] += 1
            elif "sonnet" in model_lower:
                account["calls_sonnet"] += 1
            else:
                account["calls_other"] += 1
            self._save_usage()

        logger.debug(
            f"[AccountPool] picked {account['dir']} "
            f"(usage={account['usage']:.1f}, model={model or 'default'}, weight={weight})"
        )
        return {"CLAUDE_CONFIG_DIR": account["dir"]}

    def get_stats(self) -> list[dict]:
        """Return current usage stats for all accounts."""
        return [
            {
                "dir": a["dir"],
                "usage": a["usage"],
                "calls": a["calls"],
                "calls_opus": a["calls_opus"],
                "calls_sonnet": a["calls_sonnet"],
                "calls_other": a["calls_other"],
            }
            for a in self._accounts
        ]

    @staticmethod
    def _get_weight(model: str) -> float:
        for key, weight in MODEL_WEIGHTS.items():
            if key in model.lower():
                return weight
        return DEFAULT_WEIGHT

    def _load_usage(self) -> None:
        """Load persisted usage. Reset if date changed (new day = fresh limits)."""
        if not os.path.exists(USAGE_FILE):
            return
        try:
            with open(USAGE_FILE) as f:
                data = json.load(f)

            # Reset on new day
            if data.get("date") != str(date.today()):
                logger.info("[AccountPool] new day, resetting usage counters")
                return

            saved = data.get("accounts", {})
            for account in self._accounts:
                acct_data = saved.get(account["dir"], {})
                initial = account.get("initial_usage", 0.0)
                if isinstance(acct_data, dict):
                    account["usage"] = acct_data.get("usage", 0.0) + initial
                    account["calls"] = acct_data.get("calls", 0)
                    account["calls_opus"] = acct_data.get("calls_opus", 0)
                    account["calls_sonnet"] = acct_data.get("calls_sonnet", 0)
                    account["calls_other"] = acct_data.get("calls_other", 0)
                else:
                    # Backward compat: old format stored just a float
                    account["usage"] = float(acct_data) + initial

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[AccountPool] failed to load usage: {e}")

    def _save_usage(self) -> None:
        """Persist current usage to file."""
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        data = {
            "date": str(date.today()),
            "updated": int(time.time()),
            "accounts": {
                a["dir"]: {
                    "usage": a["usage"],
                    "calls": a["calls"],
                    "calls_opus": a["calls_opus"],
                    "calls_sonnet": a["calls_sonnet"],
                    "calls_other": a["calls_other"],
                }
                for a in self._accounts
            },
        }
        try:
            with open(USAGE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            logger.warning(f"[AccountPool] failed to save usage: {e}")


def init_pool(config: dict) -> ClaudeAccountPool:
    """Initialize singleton from config.yaml agents.claude_code.accounts list."""
    global _pool
    accounts = config.get("agents", {}).get("claude_code", {}).get("accounts", [])
    _pool = ClaudeAccountPool(accounts)
    return _pool


def get_pool() -> ClaudeAccountPool:
    """Get singleton. Returns empty pool if not initialized."""
    global _pool
    if _pool is None:
        _pool = ClaudeAccountPool([])
    return _pool


# ── CLI: python -m src.auth.claude_account_pool ──

def _format_tokens(n: int) -> str:
    """Format token count: 1234567 → 1.2M, 12345 → 12.3K"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _print_usage():
    """Print today's session usage + real token stats from stats-cache.json."""

    # ── Section 1: Real token usage from Claude CLI ──
    print("=" * 70)
    print("  Claude Account Usage (from stats-cache.json)")
    print("=" * 70)

    # Discover all claude config dirs
    home = os.path.expanduser("~")
    config_dirs = []
    for name in sorted(os.listdir(home)):
        if name.startswith(".claude"):
            full = os.path.join(home, name)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, "stats-cache.json")):
                config_dirs.append(full)

    if not config_dirs:
        print("  No accounts with stats-cache.json found.\n")
    else:
        header = f"  {'Account':<16} {'Output':>10} {'Sessions':>9} {'Messages':>9}  {'Cache':>10}  {'Top Models'}"
        print(header)
        print("  " + "-" * 80)

        for config_dir in config_dirs:
            name = config_dir.split("/")[-1]
            stats = _read_stats_cache(config_dir)
            total_out = stats.get("total_output", 0)
            sessions = stats.get("total_sessions", 0)
            messages = stats.get("total_messages", 0)
            last = stats.get("last_computed", "N/A")

            # Sum cache reads
            cumulative = stats.get("cumulative_by_model", {})
            total_cache = sum(v.get("cache_read", 0) for v in cumulative.values())

            # Top models by output
            model_out = [(m, v.get("output", 0)) for m, v in cumulative.items() if v.get("output", 0) > 0]
            model_out.sort(key=lambda x: -x[1])
            model_str = ", ".join(
                f"{m.replace('claude-', '').split('-20')[0]}: {_format_tokens(t)}"
                for m, t in model_out[:3]
            )

            print(
                f"  {name:<16} {_format_tokens(total_out):>10} {sessions:>9} {messages:>9}  "
                f"{_format_tokens(total_cache):>10}  {model_str}"
            )

        print()

    # ── Section 2: Session usage (our tracking) ──
    print("=" * 70)
    print("  Agent-Mesh Session Tracking")
    print("=" * 70)

    if not os.path.exists(USAGE_FILE):
        print("  No session data yet.\n")
        return

    with open(USAGE_FILE) as f:
        data = json.load(f)

    today = str(date.today())
    file_date = data.get("date", "")
    updated = data.get("updated", 0)
    updated_str = datetime.fromtimestamp(updated).strftime("%H:%M:%S") if updated else "N/A"

    if file_date != today:
        print(f"  Last session was on {file_date} (not today). Resets on next run.\n")
        return

    print(f"  Date: {file_date}  |  Last updated: {updated_str}")
    print()

    accounts = data.get("accounts", {})
    if not accounts:
        print("  No accounts configured.\n")
        return

    total_calls = 0
    total_opus = 0
    total_sonnet = 0
    total_other = 0
    total_usage = 0.0

    rows = []
    for dir_path, acct in accounts.items():
        name = dir_path.split("/")[-1] or dir_path
        if isinstance(acct, dict):
            usage = acct.get("usage", 0.0)
            calls = acct.get("calls", 0)
            opus = acct.get("calls_opus", 0)
            sonnet = acct.get("calls_sonnet", 0)
            other = acct.get("calls_other", 0)
        else:
            usage = float(acct)
            calls = opus = sonnet = other = 0

        total_calls += calls
        total_opus += opus
        total_sonnet += sonnet
        total_other += other
        total_usage += usage
        rows.append((name, calls, opus, sonnet, other, usage))

    header = f"  {'Account':<16} {'Calls':>6} {'Opus':>6} {'Sonnet':>6} {'Other':>6} {'Weight':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, calls, opus, sonnet, other, usage in rows:
        print(f"  {name:<16} {calls:>6} {opus:>6} {sonnet:>6} {other:>6} {usage:>8.1f}")
    print("  " + "-" * (len(header) - 2))
    print(f"  {'Total':<16} {total_calls:>6} {total_opus:>6} {total_sonnet:>6} {total_other:>6} {total_usage:>8.1f}")

    if len(rows) > 1:
        usages = [r[5] for r in rows]
        max_u, min_u = max(usages), min(usages)
        avg_u = total_usage / len(rows)
        if avg_u > 0:
            imbalance = (max_u - min_u) / avg_u * 100
            label = "good" if imbalance < 20 else ("ok" if imbalance < 50 else "uneven")
            print(f"\n  Balance: max-min diff = {imbalance:.0f}% of avg ({label})")
    print()


def _reset_usage():
    """Reset session usage counters."""
    if os.path.exists(USAGE_FILE):
        os.remove(USAGE_FILE)
        print("Session counters reset.")
    else:
        print("No session data to reset.")


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv:
        _reset_usage()
    else:
        _print_usage()
