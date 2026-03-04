"""
Agent Mesh v1.0 — Spec Analyzer

Analyzes delta between spec versions using Claude CLI.
Identifies all changes (new modules, schema alterations, new APIs, etc.)
and reviews feasibility against current codebase.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent-mesh")


@dataclass
class DesignChange:
    """A single change identified in spec delta."""
    change_id: str                          # e.g. "new-module-contract"
    change_type: str                        # NEW_MODULE | ALTER_SCHEMA | NEW_API | MODIFY_BEHAVIOR | NEW_FRONTEND
    module: str                             # affected module name
    title: str                              # human-readable
    description: str                        # detailed change description
    dependencies: list[str] = field(default_factory=list)    # other change_ids this depends on
    affected_tables: list[str] = field(default_factory=list)
    affected_endpoints: list[str] = field(default_factory=list)
    estimated_complexity: str = "M"         # L/S/M/H
    spec_section: str = ""                  # relevant spec section text
    feasibility_notes: str = ""             # filled by review_feasibility

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "module": self.module,
            "title": self.title,
            "description": self.description,
            "dependencies": self.dependencies,
            "affected_tables": self.affected_tables,
            "affected_endpoints": self.affected_endpoints,
            "estimated_complexity": self.estimated_complexity,
            "spec_section": self.spec_section,
            "feasibility_notes": self.feasibility_notes,
        }

    @staticmethod
    def from_dict(d: dict) -> DesignChange:
        return DesignChange(
            change_id=d["change_id"],
            change_type=d["change_type"],
            module=d["module"],
            title=d["title"],
            description=d["description"],
            dependencies=d.get("dependencies", []),
            affected_tables=d.get("affected_tables", []),
            affected_endpoints=d.get("affected_endpoints", []),
            estimated_complexity=d.get("estimated_complexity", "M"),
            spec_section=d.get("spec_section", ""),
            feasibility_notes=d.get("feasibility_notes", ""),
        )


class SpecAnalyzer:
    """Analyzes delta between spec versions using Claude CLI."""

    def __init__(self, config: dict):
        design_cfg = config.get("design", {})
        self.model = design_cfg.get("analyzer_model", "claude-opus-4-6")

    async def analyze_delta(
        self, old_spec: str, new_spec: str, repo_dir: str
    ) -> list[DesignChange]:
        """
        Compare old vs new spec, identify all changes.
        Uses Opus to understand semantic diff (not just text diff).
        Also scans repo to understand current implementation state.
        """
        code_tree = await self._get_code_tree(repo_dir)
        prompt = self._build_delta_prompt(old_spec, new_spec, code_tree)
        raw = await self._call_claude(prompt, repo_dir)

        changes = self._parse_changes(raw)
        logger.info(f"[SpecAnalyzer] Identified {len(changes)} changes in spec delta")
        return changes

    async def review_feasibility(
        self, changes: list[DesignChange], repo_dir: str
    ) -> list[DesignChange]:
        """
        For each change, check:
        - Dependencies met?
        - Conflicts with existing code?
        - Ambiguities that need resolution?

        Annotates changes with feasibility notes and adjusts complexity.
        """
        code_tree = await self._get_code_tree(repo_dir)
        prompt = self._build_feasibility_prompt(changes, code_tree)
        raw = await self._call_claude(prompt, repo_dir)

        reviewed = self._parse_feasibility(raw, changes)
        logger.info("[SpecAnalyzer] Feasibility review complete")
        return reviewed

    def _build_delta_prompt(self, old_spec: str, new_spec: str, code_tree: str) -> str:
        return f"""You are a senior software architect analyzing spec changes.

## Task
Compare the OLD spec (v1) against the NEW spec (v2) and identify ALL changes.
Also consider the current codebase state to understand what already exists.

## Output Format
Respond ONLY with a JSON array. No other text.
Each change object:
{{
  "change_id": "kebab-case-id",
  "change_type": "NEW_MODULE" | "ALTER_SCHEMA" | "NEW_API" | "MODIFY_BEHAVIOR" | "NEW_FRONTEND",
  "module": "affected module name",
  "title": "human-readable title",
  "description": "detailed description of what changed",
  "dependencies": ["other-change-ids-this-depends-on"],
  "affected_tables": ["table names touched"],
  "affected_endpoints": ["API endpoints touched"],
  "estimated_complexity": "L" | "S" | "M" | "H",
  "spec_section": "brief excerpt of relevant new spec section"
}}

## Change Type Guide
- NEW_MODULE: entirely new module/feature not in old spec
- ALTER_SCHEMA: database table create/alter/drop
- NEW_API: new API endpoints
- MODIFY_BEHAVIOR: changed business logic in existing features
- NEW_FRONTEND: new UI pages/components

## Complexity Guide
- L: scaffolding, config, simple CRUD
- S: simple logic, single file changes
- M: business rules, auth, multi-file changes
- H: architecture, security, cross-module integration

## OLD SPEC (v1)
{old_spec}

## NEW SPEC (v2)
{new_spec}

## CURRENT CODEBASE
{code_tree}
"""

    def _build_feasibility_prompt(self, changes: list[DesignChange], code_tree: str) -> str:
        changes_json = json.dumps([c.to_dict() for c in changes], indent=2, ensure_ascii=False)
        return f"""You are a senior software architect reviewing feasibility of planned changes.

## Task
For each change below, assess:
1. Are its dependencies met (tables/modules it depends on)?
2. Any conflicts with existing code?
3. Any ambiguities that need clarification?
4. Is the complexity estimate accurate?

## Output Format
Respond ONLY with a JSON array. No other text.
Each object:
{{
  "change_id": "the-change-id",
  "feasibility_notes": "any concerns, conflicts, or clarifications needed",
  "adjusted_complexity": "L" | "S" | "M" | "H" (same or adjusted),
  "blocked": false,
  "block_reason": ""
}}

## PLANNED CHANGES
{changes_json}

## CURRENT CODEBASE
{code_tree}
"""

    async def _call_claude(self, prompt: str, cwd: str) -> str:
        """Call Claude CLI with prompt via temp file. Follows verifier.py pattern."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {self.model} --output-format text',
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=900
            )
            result = stdout.decode().strip()
            if proc.returncode != 0:
                logger.warning(
                    f"[SpecAnalyzer] Claude returned code {proc.returncode}: "
                    f"{stderr.decode()[:200]}"
                )
            return result
        except asyncio.TimeoutError:
            logger.error("[SpecAnalyzer] Claude CLI timed out (900s)")
            return "[]"
        except Exception as e:
            logger.error(f"[SpecAnalyzer] Claude CLI error: {e}")
            return "[]"
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    async def _get_code_tree(self, repo_dir: str) -> str:
        return await get_code_tree(repo_dir)

    def _parse_changes(self, raw: str) -> list[DesignChange]:
        """Parse JSON array of changes from LLM output."""
        data = _parse_json_array(raw)
        changes = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                changes.append(DesignChange.from_dict(item))
            except (KeyError, TypeError) as e:
                logger.warning(f"[SpecAnalyzer] Skipping malformed change: {e}")
        return changes

    def _parse_feasibility(
        self, raw: str, changes: list[DesignChange]
    ) -> list[DesignChange]:
        """Parse feasibility review and annotate changes."""
        data = _parse_json_array(raw)
        review_map = {item["change_id"]: item for item in data if isinstance(item, dict) and "change_id" in item}

        for change in changes:
            review = review_map.get(change.change_id)
            if review:
                change.feasibility_notes = review.get("feasibility_notes", "")
                adjusted = review.get("adjusted_complexity")
                if adjusted in ("L", "S", "M", "H"):
                    change.estimated_complexity = adjusted
                if review.get("blocked"):
                    change.feasibility_notes = (
                        f"⚠️ BLOCKED: {review.get('block_reason', 'unknown')}. "
                        + change.feasibility_notes
                    )

        return changes


async def get_code_tree(repo_dir: str, char_limit: int = 80_000) -> str:
    """Get a summary of the code tree (file listing + key file contents).

    Module-level function for reuse by DesignLoop and SpecAnalyzer.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            'find . -name "*.ts" -o -name "*.tsx" -o -name "*.prisma" -o -name "*.sol" -o -name "*.py" '
            '| grep -v node_modules | grep -v .agent-mesh | grep -v typechain | grep -v __pycache__ | sort',
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        file_list = stdout.decode().strip()

        result_parts = [f"=== File Tree ===\n{file_list}\n"]
        total_chars = len(file_list)

        for filepath in file_list.split('\n'):
            filepath = filepath.strip()
            if not filepath:
                continue
            if any(kw in filepath for kw in [
                '/services/', '/routes/', '/schemas/', '/workers/',
                'schema.prisma', '/models/', '/middleware/',
            ]):
                full_path = os.path.join(repo_dir, filepath)
                try:
                    with open(full_path, 'r') as f:
                        content = f.read()
                    if total_chars + len(content) > char_limit:
                        break
                    result_parts.append(f"\n=== {filepath} ===\n{content}")
                    total_chars += len(content)
                except Exception:
                    continue

        return '\n'.join(result_parts)
    except Exception as e:
        return f"Error getting code tree: {e}"


def _parse_json_array(raw: str) -> list:
    """Parse JSON array from LLM output, stripping markdown fences."""
    raw = raw.strip()
    if '```json' in raw:
        raw = raw.split('```json')[1].split('```')[0].strip()
    elif '```' in raw:
        raw = raw.split('```')[1].split('```')[0].strip()

    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        return [result]
    except json.JSONDecodeError:
        start = raw.find('[')
        end = raw.rfind(']')
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("[SpecAnalyzer] Cannot parse JSON from LLM output")
        return []
