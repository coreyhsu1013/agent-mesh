"""
Claude Account Pool — least-loaded CLAUDE_CONFIG_DIR for multi-account rate limit distribution.

Usage:
  pool = ClaudeAccountPool(["~/.claude", "~/.claude-b", "~/.claude-c"])
  env = await pool.next_env(model="opus")  # returns env dict with CLAUDE_CONFIG_DIR set

Query:
  python -m src.auth.claude_account_pool          # show today's usage
  python -m src.auth.claude_account_pool --reset   # reset counters

Tracks usage per account with model-based weights (opus=3, sonnet=1).
Persists usage to ~/.agent-mesh/account-usage.json, resets daily.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Model weights: heavier models consume more rate limit
MODEL_WEIGHTS = {
    "opus": 3.0,
    "sonnet": 1.0,
}
DEFAULT_WEIGHT = 1.0

USAGE_FILE = os.path.expanduser("~/.agent-mesh/account-usage.json")

# Module-level singleton
_pool: ClaudeAccountPool | None = None


class ClaudeAccountPool:
    """Least-loaded pool of Claude config directories."""

    def __init__(self, config_dirs: list[str]):
        self._accounts: list[dict] = []
        self._lock = asyncio.Lock()

        if config_dirs:
            for d in config_dirs:
                self._accounts.append({
                    "dir": os.path.expanduser(d),
                    "usage": 0.0,
                    "calls": 0,
                    "calls_opus": 0,
                    "calls_sonnet": 0,
                    "calls_other": 0,
                })
            self._load_usage()
            dirs = [a["dir"] for a in self._accounts]
            logger.info(f"[AccountPool] {len(self._accounts)} Claude accounts: {dirs}")

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
                if isinstance(acct_data, dict):
                    account["usage"] = acct_data.get("usage", 0.0)
                    account["calls"] = acct_data.get("calls", 0)
                    account["calls_opus"] = acct_data.get("calls_opus", 0)
                    account["calls_sonnet"] = acct_data.get("calls_sonnet", 0)
                    account["calls_other"] = acct_data.get("calls_other", 0)
                else:
                    # Backward compat: old format stored just a float
                    account["usage"] = float(acct_data)

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

def _print_usage():
    """Print today's account usage stats."""
    if not os.path.exists(USAGE_FILE):
        print("No usage data yet.")
        return

    with open(USAGE_FILE) as f:
        data = json.load(f)

    today = str(date.today())
    file_date = data.get("date", "")
    updated = data.get("updated", 0)
    updated_str = datetime.fromtimestamp(updated).strftime("%H:%M:%S") if updated else "N/A"

    if file_date != today:
        print(f"Last usage was on {file_date} (not today). Counters will reset on next run.")
        print()

    print(f"Date: {file_date}  |  Last updated: {updated_str}")
    print()

    accounts = data.get("accounts", {})
    if not accounts:
        print("No accounts configured.")
        return

    # Calculate totals
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
            # Backward compat
            usage = float(acct)
            calls = opus = sonnet = other = 0

        total_calls += calls
        total_opus += opus
        total_sonnet += sonnet
        total_other += other
        total_usage += usage
        rows.append((name, calls, opus, sonnet, other, usage))

    # Print table
    header = f"{'Account':<16} {'Calls':>6} {'Opus':>6} {'Sonnet':>6} {'Other':>6} {'Weight':>8}"
    print(header)
    print("-" * len(header))
    for name, calls, opus, sonnet, other, usage in rows:
        print(f"{name:<16} {calls:>6} {opus:>6} {sonnet:>6} {other:>6} {usage:>8.1f}")
    print("-" * len(header))
    print(f"{'Total':<16} {total_calls:>6} {total_opus:>6} {total_sonnet:>6} {total_other:>6} {total_usage:>8.1f}")

    # Balance indicator
    if len(rows) > 1:
        usages = [r[5] for r in rows]
        max_u, min_u = max(usages), min(usages)
        avg_u = total_usage / len(rows)
        if avg_u > 0:
            imbalance = (max_u - min_u) / avg_u * 100
            print(f"\nBalance: max-min diff = {imbalance:.0f}% of avg", end="")
            if imbalance < 20:
                print(" (good)")
            elif imbalance < 50:
                print(" (ok)")
            else:
                print(" (uneven)")


def _reset_usage():
    """Reset all usage counters."""
    if os.path.exists(USAGE_FILE):
        os.remove(USAGE_FILE)
        print("Usage counters reset.")
    else:
        print("No usage data to reset.")


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv:
        _reset_usage()
    else:
        _print_usage()
