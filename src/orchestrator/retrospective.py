"""
Agent Mesh v1.3 — Retrospective Analyzer

When gaps diverge (get worse instead of converging), this module analyzes
the root cause by comparing spec requirements vs actual code.

Triggered in verify_closed_loop() when gap count increases or stays flat.
Classifies each gap as:
  - FIXABLE: code bug, spec is clear → enrich fix task with diagnosis
  - SPEC_ISSUE: spec ambiguous/contradictory → amend spec automatically
  - UNFIXABLE: technically infeasible → remove from gap list
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger("agent-mesh")


@dataclass
class GapDiagnosis:
    gap_index: int
    gap_message: str
    root_cause: str           # FIXABLE | SPEC_ISSUE | UNFIXABLE
    analysis: str             # why this gap persists
    actual_code: str          # what the code actually does
    spec_requirement: str     # what the spec requires
    fix_strategy: str         # if FIXABLE: how to fix
    spec_amendment: str       # if SPEC_ISSUE: what spec should say


@dataclass
class RetrospectiveReport:
    diagnoses: list[GapDiagnosis] = field(default_factory=list)
    spec_amendments: list[str] = field(default_factory=list)

    @property
    def fixable_count(self) -> int:
        return sum(1 for d in self.diagnoses if d.root_cause == "FIXABLE")

    @property
    def spec_issue_count(self) -> int:
        return sum(1 for d in self.diagnoses if d.root_cause == "SPEC_ISSUE")

    @property
    def unfixable_count(self) -> int:
        return sum(1 for d in self.diagnoses if d.root_cause == "UNFIXABLE")


class RetrospectiveAnalyzer:
    """Analyzes why gaps aren't converging by comparing spec vs actual code."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir

    async def analyze(
        self,
        remaining_gaps: list[dict],
        spec_path: str,
        code_tree: str,
        cycle_history: list[dict],
    ) -> RetrospectiveReport:
        """
        Run Opus analysis on diverging gaps.

        1. Extract file paths from gap messages
        2. Read actual code snippets
        3. Compare with spec via Opus
        4. Classify each gap
        """
        if not remaining_gaps:
            return RetrospectiveReport()

        retro_cfg = self.config.get("retrospective", {})
        model = retro_cfg.get("model", "claude-opus-4-6")
        max_gaps = retro_cfg.get("max_gaps", 10)
        timeout = retro_cfg.get("timeout", 600)

        # Read spec
        spec_content = ""
        if spec_path and os.path.exists(spec_path):
            try:
                with open(spec_path) as f:
                    spec_content = f.read()
            except Exception:
                pass

        if not spec_content:
            logger.warning("[Retro] No spec content, skipping")
            return RetrospectiveReport()

        # Collect actual code snippets for files mentioned in gaps
        code_snippets = await self._collect_code_snippets(remaining_gaps[:max_gaps])

        # Build convergence history
        history_lines = []
        for entry in cycle_history:
            cycle = entry.get("cycle", "?")
            gc = entry.get("gap_count", "?")
            history_lines.append(f"  Cycle {cycle}: {gc} gaps")
        convergence_history = "\n".join(history_lines) if history_lines else "No history"

        # Build gap list
        gap_lines = []
        for idx, gap in enumerate(remaining_gaps[:max_gaps], 1):
            msg = gap.get("message", "")
            module = gap.get("module", "?")
            gap_lines.append(f"{idx}. [{module}] {msg}")
        gap_section = "\n".join(gap_lines)

        prompt = f"""You are diagnosing WHY implementation gaps are NOT converging.
After multiple fix cycles, these gaps persist or get worse.
Analyze the ACTUAL CODE vs the SPEC to find the root cause of each gap.

## CONVERGENCE HISTORY
{convergence_history}

## REMAINING GAPS
{gap_section}

## SPECIFICATION
{spec_content[:30000]}

## ACTUAL CODE (relevant files)
{code_snippets[:30000]}

## CURRENT PROJECT STRUCTURE
{code_tree[:10000]}

For EACH gap, determine:
- FIXABLE: Spec is clear, code just doesn't match → provide exact fix strategy
- SPEC_ISSUE: Spec is ambiguous/contradictory/incomplete → provide spec amendment text
- UNFIXABLE: Requirement conflicts with existing architecture → explain why

Output ONLY a JSON array. No other text.
Each item:
{{
  "gap_index": N,
  "root_cause": "FIXABLE" | "SPEC_ISSUE" | "UNFIXABLE",
  "analysis": "Why this gap persists after multiple fix attempts",
  "actual_code": "What the code currently does (brief)",
  "spec_requirement": "What the spec says (brief)",
  "fix_strategy": "If FIXABLE: exact steps to fix. Empty if not FIXABLE.",
  "spec_amendment": "If SPEC_ISSUE: corrected spec text to append. Empty if not SPEC_ISSUE."
}}
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions '
                f'--model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            raw = stdout.decode().strip()

            results = self._parse_json_array(raw)
            if not results:
                logger.warning(
                    f"[Retro] Could not parse response ({len(raw)} chars). "
                    f"First 500 chars: {raw[:500]}"
                )
                return RetrospectiveReport()

            report = RetrospectiveReport()
            gaps_list = remaining_gaps[:max_gaps]

            for item in results:
                if not isinstance(item, dict):
                    continue
                gap_idx = item.get("gap_index", 0)
                gap_msg = ""
                if 0 < gap_idx <= len(gaps_list):
                    gap_msg = gaps_list[gap_idx - 1].get("message", "")

                diagnosis = GapDiagnosis(
                    gap_index=gap_idx,
                    gap_message=gap_msg,
                    root_cause=item.get("root_cause", "FIXABLE"),
                    analysis=item.get("analysis", ""),
                    actual_code=item.get("actual_code", ""),
                    spec_requirement=item.get("spec_requirement", ""),
                    fix_strategy=item.get("fix_strategy", ""),
                    spec_amendment=item.get("spec_amendment", ""),
                )
                report.diagnoses.append(diagnosis)

                if diagnosis.root_cause == "SPEC_ISSUE" and diagnosis.spec_amendment:
                    report.spec_amendments.append(diagnosis.spec_amendment)

            logger.info(
                f"[Retro] Analysis complete: "
                f"{report.fixable_count} fixable, "
                f"{report.spec_issue_count} spec issues, "
                f"{report.unfixable_count} unfixable"
            )
            return report

        except asyncio.TimeoutError:
            logger.warning("[Retro] Analysis timed out")
            return RetrospectiveReport()
        except Exception as e:
            logger.warning(f"[Retro] Analysis failed: {e}")
            return RetrospectiveReport()
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    async def _collect_code_snippets(self, gaps: list[dict]) -> str:
        """Extract file paths from gap messages and read relevant code."""
        # Collect unique file paths from gap messages and file fields
        file_paths: set[str] = set()
        for gap in gaps:
            # From file field
            if gap.get("file"):
                file_paths.add(gap["file"])
            # From message: look for file-like patterns
            msg = gap.get("message", "")
            # Match patterns like: app/sales/service.py, packages/database/prisma/schema.prisma
            for match in re.findall(r'[\w./]+\.\w{1,5}', msg):
                if '/' in match and not match.startswith('http'):
                    file_paths.add(match)

        if not file_paths:
            return "(no specific files referenced in gaps)"

        snippets = []
        for fpath in sorted(file_paths)[:15]:  # max 15 files
            full_path = os.path.join(self.repo_dir, fpath)
            if not os.path.exists(full_path):
                continue
            try:
                with open(full_path) as f:
                    content = f.read()
                # Truncate to 200 lines
                lines = content.split('\n')
                if len(lines) > 200:
                    content = '\n'.join(lines[:200]) + '\n... (truncated)'
                snippets.append(f"### {fpath}\n```\n{content}\n```")
            except Exception:
                continue

        return "\n\n".join(snippets) if snippets else "(could not read referenced files)"

    @staticmethod
    def _parse_json_array(raw: str) -> list[dict] | None:
        """Parse JSON array from LLM output, handling markdown fences and common issues."""
        raw = raw.strip()

        # Strip markdown code fences
        if "```" in raw:
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        # Try direct parse
        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Find the outermost [ ... ] by bracket matching
        start = raw.find("[")
        if start >= 0:
            depth = 0
            end = -1
            for i in range(start, len(raw)):
                if raw[i] == "[":
                    depth += 1
                elif raw[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                candidate = raw[start:end]
                try:
                    result = json.loads(candidate)
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    pass

                # Fix trailing commas before ] or }
                fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
                try:
                    result = json.loads(fixed)
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    pass

        return None
