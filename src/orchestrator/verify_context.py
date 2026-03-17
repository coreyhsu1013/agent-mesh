"""
Agent Mesh v2.1 — Verify Context Loader

Loads verification context for a cycle:
- Determines effective spec path (partial for chunk, full otherwise)
- Resolves scope modules from chunk_id
- Provides exclude_modules from config

Used by project_loop.py and verifier.py to scope verification.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("agent-mesh")


@dataclass
class VerifyContext:
    chunk_id: str = ""
    spec_path: str = ""             # effective spec (partial for chunk, full otherwise)
    full_spec_path: str = ""        # always the full spec
    scope_modules: list[str] = field(default_factory=list)
    exclude_modules: list[str] = field(default_factory=list)
    is_chunk_run: bool = False
    # v2.1 refinement
    freeze_spec_version: str = ""                          # spec SHA/timestamp for consistency
    active_amendments: list[str] = field(default_factory=list)  # current cycle's spec amendments
    verifier_exclusions: list[str] = field(default_factory=list)  # patterns to skip in verify

    def effective_spec_path(self) -> str:
        """Return the spec path to use for verification."""
        return self.spec_path or self.full_spec_path

    def scope_instruction(self) -> str:
        """Build an LLM instruction for scoped verification."""
        parts: list[str] = []

        if self.is_chunk_run and self.scope_modules:
            modules_str = ", ".join(self.scope_modules)
            parts.append(
                f"IMPORTANT: Only report gaps related to these modules: {modules_str}. "
                f"Ignore everything outside this scope."
            )

        if self.active_amendments:
            amendments_str = "; ".join(self.active_amendments)
            parts.append(
                f"AMENDMENTS: The following spec changes are in effect: {amendments_str}"
            )

        if self.verifier_exclusions:
            excl_str = ", ".join(self.verifier_exclusions)
            parts.append(
                f"EXCLUDE: Do NOT flag issues in: {excl_str}"
            )

        return "\n".join(parts)


class VerifyContextLoader:
    """Loads verification context for a cycle."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir

    def load(self, chunk_id: str = "", spec_path: str = "") -> VerifyContext:
        """Load verify context from config + chunk_id."""
        verify_cfg = self.config.get("verify", {})
        exclude_modules = verify_cfg.get("exclude_modules", [])
        verifier_exclusions = verify_cfg.get("verifier_exclusions", [])

        ctx = VerifyContext(
            chunk_id=chunk_id,
            full_spec_path=spec_path,
            exclude_modules=exclude_modules,
            verifier_exclusions=verifier_exclusions,
        )

        # Freeze spec version for consistency
        if spec_path and os.path.exists(spec_path):
            ctx.freeze_spec_version = str(int(os.path.getmtime(spec_path)))

        # Load active amendments from .agent-mesh/amendments/
        amendments_dir = os.path.join(self.repo_dir, ".agent-mesh", "amendments")
        if os.path.isdir(amendments_dir):
            for f in sorted(os.listdir(amendments_dir)):
                if f.endswith(".md"):
                    path = os.path.join(amendments_dir, f)
                    try:
                        with open(path) as fh:
                            first_line = fh.readline().strip()
                            if first_line:
                                ctx.active_amendments.append(first_line)
                    except Exception:
                        pass

        if chunk_id:
            ctx.is_chunk_run = True
            # Try to find chunk-specific spec
            chunk_spec = os.path.join(
                self.repo_dir, ".agent-mesh", f"{chunk_id}-spec.md"
            )
            if os.path.exists(chunk_spec):
                ctx.spec_path = chunk_spec
                logger.info(
                    f"[VerifyContext] Using chunk spec: {chunk_spec}"
                )
            else:
                ctx.spec_path = spec_path  # fallback to full spec

            # Resolve scope modules
            ctx.scope_modules = list(self._resolve_scope_modules(chunk_id))
        else:
            ctx.spec_path = spec_path

        logger.info(
            f"[VerifyContext] chunk={chunk_id or 'none'}, "
            f"is_chunk={ctx.is_chunk_run}, "
            f"spec={'chunk' if ctx.spec_path != spec_path else 'full'}, "
            f"scope_modules={ctx.scope_modules or 'all'}"
        )

        return ctx

    def _resolve_scope_modules(self, chunk_id: str) -> set[str]:
        """Extract scope modules from chunk_id and spec file."""
        modules: set[str] = set()

        # From chunk_id: "chunk-4-notification-backend" → "notification"
        parts = chunk_id.split("-")[2:]
        for part in parts:
            if part not in ("backend", "frontend", "api", "schema", "dependent",
                            "foundation", "and"):
                modules.add(part.lower())

        # From spec header
        spec_path = os.path.join(
            self.repo_dir, ".agent-mesh", f"{chunk_id}-spec.md"
        )
        if os.path.exists(spec_path):
            try:
                with open(spec_path) as f:
                    header = f.read(500)
                for match in re.findall(r'(?:scope|module|chunk)[:\s]+([^\n]+)', header, re.I):
                    for word in match.split(","):
                        word = word.strip().lower()
                        word = re.sub(r'[^a-z0-9]', '', word)
                        if word and len(word) > 2:
                            modules.add(word)
            except Exception:
                pass

        return modules
