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

logger = logging.getLogger(__name__)


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
