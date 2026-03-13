"""
Agent Mesh — CLI Runner
Executes prompts via CLI tools using temp file + shell pipe.
All LLM calls go through authenticated CLI (no API keys needed for Claude/Gemini).
"""

from __future__ import annotations
import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

from src.auth.claude_account_pool import get_pool

logger = logging.getLogger(__name__)

# Keys to strip from child process env to avoid nested-session errors.
# CLAUDE_CODE_SESSION_ACCESS_TOKEN causes child CLI to use parent's session
# auth instead of local OAuth, resulting in "Not logged in" errors.
_STRIP_ENV_KEYS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
)


def build_proc_env(extra: dict | None = None) -> dict:
    """Build env dict for subprocess, stripping Claude Code nesting guards."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


@dataclass
class RunResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


async def run_claude_prompt(prompt: str, cwd: str, timeout: int = 300,
                            output_format: str = "text") -> RunResult:
    """
    Execute a prompt via Claude CLI using temp file + shell pipe.
    Avoids OS argument length limit by writing prompt to temp file.
    Uses --dangerously-skip-permissions for non-interactive execution.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = (
            f"cat {prompt_file} | claude -p "
            f"--dangerously-skip-permissions "
            f"--output-format {output_format}"
        )

        logger.debug(f"[ClaudeRunner] cwd={cwd}, prompt_len={len(prompt)}")

        # Multi-account: inject CLAUDE_CONFIG_DIR
        account_env = await get_pool().next_env()
        proc_env = build_proc_env(account_env)

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return RunResult(
                success=False,
                error=f"Timeout after {timeout}s",
            )

        stdout = stdout_bytes.decode(errors="replace")[:5000]
        stderr = stderr_bytes.decode(errors="replace")[:2000]
        success = proc.returncode == 0

        if not success:
            logger.warning(f"[ClaudeRunner] Exit {proc.returncode}: {stderr[:300]}")

        return RunResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            error=None if success else f"Exit {proc.returncode}: {stderr[:500]}",
        )
    finally:
        os.unlink(prompt_file)


async def run_gemini_prompt(prompt: str, cwd: str, timeout: int = 300) -> RunResult:
    """
    Execute a prompt via Gemini CLI using temp file + shell pipe.
    Verified: Gemini CLI supports pipe mode (cat prompt | gemini).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = f"cat {prompt_file} | gemini"

        logger.debug(f"[GeminiRunner] cwd={cwd}, prompt_len={len(prompt)}")

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return RunResult(success=False, error=f"Timeout after {timeout}s")

        stdout = stdout_bytes.decode(errors="replace")[:5000]
        stderr = stderr_bytes.decode(errors="replace")[:2000]
        success = proc.returncode == 0

        return RunResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            error=None if success else f"Exit {proc.returncode}: {stderr[:500]}",
        )
    finally:
        os.unlink(prompt_file)
