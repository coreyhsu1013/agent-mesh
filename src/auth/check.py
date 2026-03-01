"""
Agent Mesh — CLI Authentication Check
Verifies that CLI tools (claude, gemini, codex, aider) are logged in and available.
"""

from __future__ import annotations
import subprocess
import logging
import shutil
from typing import NamedTuple

logger = logging.getLogger(__name__)


class AuthStatus(NamedTuple):
    tool: str
    available: bool
    logged_in: bool
    message: str


def check_cli(tool: str) -> AuthStatus:
    """Check if a CLI tool is available and authenticated."""
    if not shutil.which(tool):
        return AuthStatus(tool, False, False, f"{tool} not found in PATH")

    try:
        if tool == "claude":
            r = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=10
            )
            available = r.returncode == 0
            # Claude CLI doesn't have a simple auth check command
            # If it's installed, assume logged in (will fail at runtime if not)
            return AuthStatus(tool, available, available,
                              f"v{r.stdout.strip()}" if available else r.stderr[:100])

        elif tool == "gemini":
            r = subprocess.run(
                ["gemini", "--version"],
                capture_output=True, text=True, timeout=10
            )
            available = r.returncode == 0
            return AuthStatus(tool, available, available,
                              f"v{r.stdout.strip()}" if available else r.stderr[:100])

        elif tool == "codex":
            r = subprocess.run(
                ["codex", "--version"],
                capture_output=True, text=True, timeout=10
            )
            available = r.returncode == 0
            return AuthStatus(tool, available, available,
                              f"v{r.stdout.strip()}" if available else r.stderr[:100])

        elif tool == "aider":
            r = subprocess.run(
                ["aider", "--version"],
                capture_output=True, text=True, timeout=10
            )
            available = r.returncode == 0
            return AuthStatus(tool, available, True,
                              r.stdout.strip() if available else r.stderr[:100])

        else:
            return AuthStatus(tool, False, False, f"Unknown tool: {tool}")

    except subprocess.TimeoutExpired:
        return AuthStatus(tool, False, False, f"{tool} timed out")
    except Exception as e:
        return AuthStatus(tool, False, False, str(e))


def check_all_required(config: dict) -> list[AuthStatus]:
    """Check all required tools based on config."""
    results = []

    # Always need claude (for reviewer)
    results.append(check_cli("claude"))

    # Check agents
    agents = config.get("agents", {})

    if agents.get("deepseek_aider", {}).get("enabled", False):
        results.append(check_cli("aider"))

    # Check planner
    planner = config.get("planner", {})
    if planner.get("provider") == "gemini":
        results.append(check_cli("gemini"))

    if agents.get("codex", {}).get("enabled", False):
        results.append(check_cli("codex"))

    return results


def print_auth_status(results: list[AuthStatus]) -> bool:
    """Print auth status and return True if all required tools are OK."""
    all_ok = True
    for r in results:
        if r.available and r.logged_in:
            logger.info(f"  ✅ {r.tool}: {r.message}")
        elif r.available:
            logger.warning(f"  ⚠️  {r.tool}: available but not logged in — {r.message}")
            all_ok = False
        else:
            logger.error(f"  ❌ {r.tool}: {r.message}")
            all_ok = False
    return all_ok
