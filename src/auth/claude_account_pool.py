"""
Claude Account Pool — round-robin CLAUDE_CONFIG_DIR for multi-account rate limit distribution.

Usage:
  pool = ClaudeAccountPool(["~/.claude", "~/.claude-b", "~/.claude-c"])
  env = await pool.next_env()  # returns env dict with CLAUDE_CONFIG_DIR set
"""

from __future__ import annotations
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Module-level singleton
_pool: ClaudeAccountPool | None = None


class ClaudeAccountPool:
    """Round-robin pool of Claude config directories."""

    def __init__(self, config_dirs: list[str]):
        self._dirs = [os.path.expanduser(d) for d in config_dirs] if config_dirs else []
        self._index = 0
        self._lock = asyncio.Lock()
        if self._dirs:
            logger.info(f"[AccountPool] {len(self._dirs)} Claude accounts: {self._dirs}")

    async def next_env(self) -> dict[str, str]:
        """Return env dict overlay for next account. Empty dict = use default."""
        if not self._dirs:
            return {}
        async with self._lock:
            d = self._dirs[self._index % len(self._dirs)]
            self._index += 1
        return {"CLAUDE_CONFIG_DIR": d}


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
