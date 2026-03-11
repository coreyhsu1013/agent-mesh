"""
Claude Account Pool — least-loaded CLAUDE_CONFIG_DIR for multi-account rate limit distribution.

Usage:
  pool = ClaudeAccountPool(["~/.claude", "~/.claude-b", "~/.claude-c"])
  env = await pool.next_env(model="opus")  # returns env dict with CLAUDE_CONFIG_DIR set

Tracks usage per account with model-based weights (opus=3, sonnet=1).
Persists usage to ~/.agent-mesh/account-usage.json, resets daily.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from datetime import date

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
                })
            self._load_usage()
            dirs = [a["dir"] for a in self._accounts]
            logger.info(f"[AccountPool] {len(self._accounts)} Claude accounts: {dirs}")

    async def next_env(self, model: str = "") -> dict[str, str]:
        """Return env dict for least-used account. Empty dict = use default."""
        if not self._accounts:
            return {}

        weight = self._get_weight(model)

        async with self._lock:
            # Pick account with minimum usage
            account = min(self._accounts, key=lambda a: a["usage"])
            account["usage"] += weight
            self._save_usage()

        logger.debug(
            f"[AccountPool] picked {account['dir']} "
            f"(usage={account['usage']:.1f}, model={model or 'default'}, weight={weight})"
        )
        return {"CLAUDE_CONFIG_DIR": account["dir"]}

    def get_stats(self) -> list[dict]:
        """Return current usage stats for all accounts."""
        return [{"dir": a["dir"], "usage": a["usage"]} for a in self._accounts]

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
                account["usage"] = saved.get(account["dir"], 0.0)

            logger.info(f"[AccountPool] loaded usage: {saved}")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[AccountPool] failed to load usage: {e}")

    def _save_usage(self) -> None:
        """Persist current usage to file."""
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        data = {
            "date": str(date.today()),
            "updated": int(time.time()),
            "accounts": {a["dir"]: a["usage"] for a in self._accounts},
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
