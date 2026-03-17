"""
Agent Mesh v1.2 — Verifier
Runs mechanical checks (conflict scan, build, test, lint) and
LLM-powered spec diff (single model, configurable).

Verify steps (ordered by cost):
  1. Conflict markers scan        — 0 tokens, instant
  2. Build (tsc / hardhat compile) — 0 tokens, seconds
  3. Lint                          — 0 tokens, seconds
  4. Test                          — 0 tokens, seconds
  5. Spec diff (single model)     — $$ tokens, minutes

v1.2: Single model verify (was dual Gemini+Opus), code tree cache, spec cache.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent-mesh")


# ── Data Structures ──

@dataclass
class VerifyIssue:
    """Single issue found during verification."""
    category: str        # conflict | build | lint | test | spec_gap | ac_fail
    severity: str        # HIGH | MEDIUM | LOW
    message: str
    file: str | None = None
    module: str | None = None
    found_by: list[str] = field(default_factory=lambda: ["mechanical"])

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "module": self.module,
            "found_by": self.found_by,
        }


@dataclass
class VerifyReport:
    """Full verification report for a cycle."""
    cycle: int
    issues: list[VerifyIssue] = field(default_factory=list)
    build_ok: bool = False
    test_ok: bool = False
    lint_ok: bool = False
    spec_gap_count: int = 0
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return len(self.issues) == 0

    @property
    def high_issues(self) -> list[VerifyIssue]:
        return [i for i in self.issues if i.severity == "HIGH"]

    def summary(self) -> str:
        status = "✅ PASSED" if self.passed else f"❌ {len(self.issues)} issues"
        parts = [
            f"[Verify Cycle {self.cycle}] {status}",
            f"  Build: {'✅' if self.build_ok else '❌'}",
            f"  Test:  {'✅' if self.test_ok else '❌'}",
            f"  Lint:  {'✅' if self.lint_ok else '❌'}",
            f"  Spec gaps: {self.spec_gap_count}",
            f"  Duration: {self.duration_s:.1f}s",
        ]
        if self.issues:
            parts.append("  Issues:")
            for i in self.issues[:10]:
                parts.append(f"    [{i.severity}] {i.category}: {i.message}")
            if len(self.issues) > 10:
                parts.append(f"    ... and {len(self.issues) - 10} more")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle,
            "passed": self.passed,
            "build_ok": self.build_ok,
            "test_ok": self.test_ok,
            "lint_ok": self.lint_ok,
            "spec_gap_count": self.spec_gap_count,
            "issue_count": len(self.issues),
            "high_count": len(self.high_issues),
            "duration_s": self.duration_s,
            "issues": [i.to_dict() for i in self.issues],
        }


# ── Verifier ──

class Verifier:
    """Runs verification checks on a repo."""

    def __init__(self, repo_dir: str, config: dict):
        self.repo_dir = repo_dir
        self.config = config
        verify_cfg = config.get("verify", {})
        self.build_cmd = verify_cfg.get("build_cmd", "pnpm build")
        self.test_cmd = verify_cfg.get("test_cmd", "pnpm test")
        self.lint_cmd = verify_cfg.get("lint_cmd", "pnpm lint")
        self.skip_lint = verify_cfg.get("skip_lint", True)  # many projects don't have lint
        self.skip_test = verify_cfg.get("skip_test", False)

        # v1.2: Caches (avoid redundant I/O and LLM calls within a cycle)
        self._code_tree_cache: str | None = None
        self._code_tree_ts: float = 0
        self._spec_cache: dict[str, str] = {}  # path → content

    async def run(self, cycle: int = 1, spec_path: str | None = None) -> VerifyReport:
        """Run all verification steps and return report."""
        t0 = time.time()
        report = VerifyReport(cycle=cycle)

        # Step 1: Conflict markers (instant, free)
        conflicts = await self._scan_conflicts()
        for f in conflicts:
            report.issues.append(VerifyIssue(
                category="conflict",
                severity="HIGH",
                message=f"Git conflict markers in {f}",
                file=f,
            ))

        # Step 2: Build
        build_ok, build_errors = await self._run_build()
        report.build_ok = build_ok
        if not build_ok:
            for err in build_errors[:20]:  # cap at 20
                report.issues.append(VerifyIssue(
                    category="build",
                    severity="HIGH",
                    message=err,
                ))

        # Step 3: Lint (optional)
        if not self.skip_lint:
            lint_ok, lint_warnings = await self._run_lint()
            report.lint_ok = lint_ok
            if not lint_ok:
                for w in lint_warnings[:10]:
                    report.issues.append(VerifyIssue(
                        category="lint",
                        severity="MEDIUM",
                        message=w,
                    ))
        else:
            report.lint_ok = True

        # Step 4: Test (optional)
        if not self.skip_test:
            test_ok, test_failures = await self._run_tests()
            report.test_ok = test_ok
            if not test_ok:
                for f in test_failures[:10]:
                    report.issues.append(VerifyIssue(
                        category="test",
                        severity="HIGH",
                        message=f,
                    ))
        else:
            report.test_ok = True

        # Step 5-6: Spec diff (LLM) — only if spec provided and mechanical checks pass
        if spec_path and report.build_ok:
            spec_issues = await self._spec_diff(spec_path)
            report.issues.extend(spec_issues)
            report.spec_gap_count = len(spec_issues)

        report.duration_s = time.time() - t0
        return report

    async def run_mechanical(self, cycle: int = 1) -> VerifyReport:
        """Run only mechanical checks (conflicts, build, test, lint). No LLM."""
        t0 = time.time()
        report = VerifyReport(cycle=cycle)

        # Step 1: Conflict markers
        conflicts = await self._scan_conflicts()
        for f in conflicts:
            report.issues.append(VerifyIssue(
                category="conflict",
                severity="HIGH",
                message=f"Git conflict markers in {f}",
                file=f,
            ))

        # Step 2: Build
        build_ok, build_errors = await self._run_build()
        report.build_ok = build_ok
        if not build_ok:
            for err in build_errors[:20]:
                report.issues.append(VerifyIssue(
                    category="build",
                    severity="HIGH",
                    message=err,
                ))

        # Step 3: Lint (optional)
        if not self.skip_lint:
            lint_ok, lint_warnings = await self._run_lint()
            report.lint_ok = lint_ok
            if not lint_ok:
                for w in lint_warnings[:10]:
                    report.issues.append(VerifyIssue(
                        category="lint",
                        severity="MEDIUM",
                        message=w,
                    ))
        else:
            report.lint_ok = True

        # Step 4: Test (optional)
        if not self.skip_test:
            test_ok, test_failures = await self._run_tests()
            report.test_ok = test_ok
            if not test_ok:
                for f in test_failures[:10]:
                    report.issues.append(VerifyIssue(
                        category="test",
                        severity="HIGH",
                        message=f,
                    ))
        else:
            report.test_ok = True

        report.duration_s = time.time() - t0
        return report

    async def run_regression(
        self,
        prev_gaps: list[dict],
        spec_path: str,
        code_tree: str,
    ) -> list[dict]:
        """
        Regression check: verify whether previously identified gaps have been fixed.
        Uses Sonnet (cheaper) since this is a yes/no checklist.
        Returns only gaps that are NOT fixed.
        """
        if not prev_gaps:
            return []

        verify_cfg = self.config.get("verify", {})
        model = verify_cfg.get("regression_model", "claude-sonnet-4-6")

        # Build checklist of previous gaps
        gap_lines = []
        for idx, gap in enumerate(prev_gaps, 1):
            msg = gap.get("message", "")
            module = gap.get("module", "?")
            gap_lines.append(f"{idx}. [{module}] {msg}")
        gap_checklist = "\n".join(gap_lines)

        prompt = f"""You are verifying whether previously identified gaps have been fixed.
For each gap below, check the ACTUAL CODE and respond with ONLY a JSON array.
Each item: {{"index": N, "status": "FIXED" | "REMAINING" | "PARTIAL", "evidence": "brief explanation"}}

## PREVIOUS GAPS
{gap_checklist}

## ACTUAL CODE
{code_tree}
"""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            raw = stdout.decode().strip()

            # Parse JSON response
            results = self._parse_json_array(raw)
            if not results:
                logger.warning("[Verifier] Regression check: could not parse response, treating all as remaining")
                return prev_gaps

            # Filter: return only gaps that are NOT fixed
            remaining = []
            fixed_indices = set()
            for item in results:
                if not isinstance(item, dict):
                    continue
                idx = item.get("index", 0)
                status = item.get("status", "REMAINING")
                if status == "FIXED":
                    fixed_indices.add(idx)

            for idx, gap in enumerate(prev_gaps, 1):
                if idx not in fixed_indices:
                    remaining.append(gap)

            logger.info(
                f"[Verifier] Regression: {len(fixed_indices)} fixed, "
                f"{len(remaining)} remaining out of {len(prev_gaps)}"
            )
            return remaining

        except asyncio.TimeoutError:
            logger.warning("[Verifier] Regression check timed out, treating all as remaining")
            return prev_gaps
        except Exception as e:
            logger.warning(f"[Verifier] Regression check failed: {e}")
            return prev_gaps
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    async def run_bounded_scan(
        self,
        spec_path: str,
        code_tree: str,
        exclude_modules: list[str],
        max_gaps: int = 5,
        known_gaps: list[dict] | None = None,
        verify_context: "VerifyContext | None" = None,
    ) -> list[VerifyIssue]:
        """
        Bounded scan for NEW critical gaps not previously identified.
        Uses Opus for thoroughness but caps output.
        known_gaps: list of remaining gaps from regression — excluded from scan.
        """
        verify_cfg = self.config.get("verify", {})
        model = verify_cfg.get("scan_model", "claude-opus-4-6")

        # v2.1: use verify_context for scoped verification
        effective_spec = spec_path
        scope_instruction = ""
        if verify_context:
            effective_spec = verify_context.effective_spec_path() or spec_path
            scope_instruction = verify_context.scope_instruction()
            if verify_context.exclude_modules:
                exclude_modules = list(set(exclude_modules) | set(verify_context.exclude_modules))

        exclude_str = ", ".join(exclude_modules) if exclude_modules else "none"

        # Build known gaps section so LLM doesn't re-report them
        known_section = ""
        if known_gaps:
            known_lines = []
            for idx, gap in enumerate(known_gaps, 1):
                msg = gap.get("message", "")
                module = gap.get("module", "?")
                known_lines.append(f"{idx}. [{module}] {msg}")
            known_section = (
                "\n## ALREADY KNOWN GAPS (do NOT re-report these)\n"
                + "\n".join(known_lines)
                + "\n"
            )

        spec_content = self._read_spec(effective_spec)
        if not spec_content:
            # fallback to original spec_path
            spec_content = self._read_spec(spec_path)
            if not spec_content:
                return []

        prompt = f"""You are scanning for NEW critical gaps not previously identified.

## SCOPE RESTRICTION (CRITICAL)
You are verifying a PARTIAL specification for one chunk of a larger project.
ONLY report gaps for requirements that are EXPLICITLY listed in the SPECIFICATION section below.
Do NOT report issues for features, modules, or requirements not mentioned in this specification.
If a feature is not mentioned in the spec, it is handled by a different chunk — IGNORE it completely.
{scope_instruction}
## CONSTRAINTS
- Only report HIGH severity gaps that affect core functionality
- Maximum {max_gaps} new gaps
- EXCLUDE these modules entirely: {exclude_str}
- Do NOT re-report issues that are already partially implemented
- Do NOT re-report any issue listed in ALREADY KNOWN GAPS below
- Focus on completely MISSING functionality only
- IMPORTANT: If a requirement is not in the SPECIFICATION below, do NOT report it as a gap
{known_section}
## Output Format
Respond ONLY with a JSON array (max {max_gaps} items). No other text.
Each item:
{{
  "module": "Module name from spec",
  "requirement": "Specific requirement from spec",
  "status": "NOT_IMPLEMENTED" | "PARTIAL" | "INCORRECT",
  "evidence": "What you see (or don't see) in the code",
  "severity": "HIGH",
  "suggested_fix": "Brief description of what needs to be done"
}}

If no new gaps, return: []

## SPECIFICATION
{spec_content}

## ACTUAL CODE
{code_tree}
"""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
            issues = self._parse_gap_json(stdout.decode(), source="bounded-scan")

            # Cap at max_gaps
            if len(issues) > max_gaps:
                logger.info(f"[Verifier] Bounded scan returned {len(issues)}, capping at {max_gaps}")
                issues = issues[:max_gaps]

            return issues

        except asyncio.TimeoutError:
            logger.warning("[Verifier] Bounded scan timed out")
            return []
        except Exception as e:
            logger.warning(f"[Verifier] Bounded scan failed: {e}")
            return []
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    async def run_spec_feedback(
        self,
        stuck_gaps: list[dict],
        spec_path: str | None,
        code_tree: str,
    ) -> list[VerifyIssue]:
        """
        Layer 3: Analyze stuck gaps — is it a code problem or a spec problem?

        For each stuck gap, ask Opus to classify:
        - CODE_BUG: the spec is clear, agent just didn't implement correctly
        - SPEC_AMBIGUOUS: spec wording is unclear → suggest clarification
        - SPEC_CONTRADICTION: spec conflicts with itself → suggest correction
        - SPEC_IMPOSSIBLE: spec requires something technically infeasible
        """
        if not stuck_gaps:
            return []

        layer3_cfg = self.config.get("layer3", {})
        model = layer3_cfg.get("model", "claude-opus-4-6")
        max_items = layer3_cfg.get("max_feedback_items", 3)

        spec_content = self._read_spec(spec_path) if spec_path else ""
        if not spec_content:
            return []

        # Build stuck gaps section
        gap_lines = []
        for idx, gap in enumerate(stuck_gaps, 1):
            msg = gap.get("message", "")
            module = gap.get("module", "?")
            cycles = gap.get("stuck_cycles", "?")
            gap_lines.append(f"{idx}. [{module}] {msg} (stuck for {cycles} cycles)")
        gap_section = "\n".join(gap_lines)

        prompt = f"""You are analyzing WHY these implementation gaps persist after multiple fix cycles.
Agents have tried to fix these gaps multiple times but they keep reappearing.
For each gap, determine the ROOT CAUSE:

- CODE_BUG: Spec is clear, agent just didn't implement correctly → keep trying to fix code
- SPEC_AMBIGUOUS: Spec wording allows multiple interpretations → suggest clarification
- SPEC_CONTRADICTION: Spec says X in one place and Y in another → identify both places
- SPEC_IMPOSSIBLE: Requirement is technically infeasible with current architecture → explain why

## STUCK GAPS (persisted multiple cycles)
{gap_section}

## SPECIFICATION
{spec_content}

## CURRENT CODE
{code_tree}

Output ONLY a JSON array (max {max_items} items). No other text.
Each item:
{{
  "gap_index": N,
  "root_cause": "CODE_BUG" | "SPEC_AMBIGUOUS" | "SPEC_CONTRADICTION" | "SPEC_IMPOSSIBLE",
  "analysis": "Detailed explanation of why this gap persists",
  "suggestion": "Concrete action to resolve (spec edit or code fix)",
  "spec_section": "The relevant section of the spec (if applicable)"
}}
"""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
            raw = stdout.decode().strip()

            results = self._parse_json_array(raw)
            if not results:
                logger.warning("[Layer3] Could not parse spec feedback response")
                return []

            issues = []
            for item in results[:max_items]:
                if not isinstance(item, dict):
                    continue
                root_cause = item.get("root_cause", "CODE_BUG")
                analysis = item.get("analysis", "")
                suggestion = item.get("suggestion", "")
                gap_idx = item.get("gap_index", 0)

                # Map root_cause to issue category
                if root_cause == "CODE_BUG":
                    # Keep as spec_gap — agent needs to try harder
                    continue
                elif root_cause in ("SPEC_AMBIGUOUS", "SPEC_CONTRADICTION"):
                    issues.append(VerifyIssue(
                        category="spec_feedback",
                        severity="MEDIUM",
                        message=f"[{root_cause}] {analysis} → {suggestion}",
                        module=stuck_gaps[gap_idx - 1].get("module") if gap_idx > 0 else None,
                        found_by=["layer3"],
                    ))
                elif root_cause == "SPEC_IMPOSSIBLE":
                    issues.append(VerifyIssue(
                        category="spec_question",
                        severity="HIGH",
                        message=f"[SPEC_IMPOSSIBLE] {analysis} → {suggestion}",
                        module=stuck_gaps[gap_idx - 1].get("module") if gap_idx > 0 else None,
                        found_by=["layer3"],
                    ))

            logger.info(
                f"[Layer3] Spec feedback: {len(results)} analyzed, "
                f"{len(issues)} spec issues found"
            )
            return issues

        except asyncio.TimeoutError:
            logger.warning("[Layer3] Spec feedback timed out")
            return []
        except Exception as e:
            logger.warning(f"[Layer3] Spec feedback failed: {e}")
            return []
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    async def run_integration_check(
        self,
        spec_path: str | None,
        code_tree: str,
    ) -> list[VerifyIssue]:
        """
        Layer 4: Check cross-module integration.
        - Type imports across modules: do consumers match providers?
        - API route contracts: do frontend calls match backend endpoints?
        - Shared types/schemas: are they consistent?
        """
        layer4_cfg = self.config.get("layer4", {})
        model = layer4_cfg.get("model", "claude-sonnet-4-6")

        spec_content = self._read_spec(spec_path) if spec_path else ""

        prompt = f"""You are checking CROSS-MODULE INTEGRATION issues.
Focus on connections BETWEEN modules, not within a single module.

Check for:
1. Type mismatches: Module A exports type X, Module B imports and uses it differently
2. API contract breaks: Frontend calls POST /api/foo with {{bar}}, backend expects {{baz}}
3. Missing imports: Module A needs function from Module B but import is missing/wrong
4. Schema drift: Prisma schema defines field X, but API/frontend uses different name
5. Shared constant inconsistency: Same enum/constant defined differently in two places

Only report REAL integration issues that would cause runtime errors or type errors.
Do NOT report issues within a single module.
Maximum 5 issues.

## SPECIFICATION
{spec_content}

## CODE
{code_tree}

Output ONLY a JSON array (max 5 items). No other text.
Each item:
{{
  "modules": ["module_a", "module_b"],
  "type": "type_mismatch" | "api_contract" | "missing_import" | "schema_drift" | "constant_inconsistency",
  "severity": "HIGH",
  "message": "Description of the cross-module issue",
  "affected_files": ["path/to/file1.ts", "path/to/file2.ts"],
  "suggested_fix": "Brief description of how to fix"
}}

If no integration issues, return: []
"""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            raw = stdout.decode().strip()

            results = self._parse_json_array(raw)
            if not results:
                logger.warning("[Layer4] Could not parse integration check response")
                return []

            issues = []
            for item in results[:5]:
                if not isinstance(item, dict):
                    continue
                modules = item.get("modules", [])
                module_str = " × ".join(modules) if modules else "cross-module"
                issues.append(VerifyIssue(
                    category="integration",
                    severity=item.get("severity", "HIGH"),
                    message=f"[{item.get('type', '?')}] {item.get('message', '')}",
                    module=module_str,
                    found_by=["layer4"],
                ))

            logger.info(f"[Layer4] Integration check: {len(issues)} issues found")
            return issues

        except asyncio.TimeoutError:
            logger.warning("[Layer4] Integration check timed out")
            return []
        except Exception as e:
            logger.warning(f"[Layer4] Integration check failed: {e}")
            return []
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    def _parse_json_array(self, raw: str) -> list[dict] | None:
        """Parse a JSON array from LLM output, handling markdown fences."""
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
                    return None
            return None

    # ── Mechanical Checks ──

    async def _scan_conflicts(self) -> list[str]:
        """Scan for git conflict markers in source files."""
        try:
            proc = await asyncio.create_subprocess_shell(
                'grep -rl "<<<<<<< " --include="*.ts" --include="*.tsx" '
                '--include="*.js" --include="*.json" --include="*.prisma" '
                '--include="*.sol" --include="*.yaml" --include="*.yml" '
                '| grep -v node_modules | grep -v .agent-mesh',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if stdout:
                return [f.strip() for f in stdout.decode().strip().split('\n') if f.strip()]
            return []
        except Exception:
            return []

    async def _run_build(self) -> tuple[bool, list[str]]:
        """Run build command, return (success, error_lines)."""
        return await self._run_cmd(self.build_cmd)

    async def _run_lint(self) -> tuple[bool, list[str]]:
        """Run lint command, return (success, warning_lines)."""
        return await self._run_cmd(self.lint_cmd)

    async def _run_tests(self) -> tuple[bool, list[str]]:
        """Run test command, return (success, failure_lines)."""
        return await self._run_cmd(self.test_cmd)

    async def _run_cmd(self, cmd: str) -> tuple[bool, list[str]]:
        """Run a shell command, return (success, relevant_output_lines)."""
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300
            )
            output = (stdout.decode() + "\n" + stderr.decode()).strip()
            success = proc.returncode == 0

            if not success:
                # Extract error lines
                errors = []
                for line in output.split('\n'):
                    line = line.strip()
                    if any(kw in line.lower() for kw in ['error', 'failed', 'fail', 'cannot find']):
                        if len(line) > 200:
                            line = line[:200] + "..."
                        errors.append(line)
                return False, errors[:20]

            return True, []

        except asyncio.TimeoutError:
            return False, [f"Command timed out after 300s: {cmd}"]
        except Exception as e:
            return False, [f"Command failed: {e}"]

    # ── LLM Spec Diff ──

    async def _spec_diff(self, spec_path: str) -> list[VerifyIssue]:
        """
        LLM-powered spec diff: compare spec vs actual code.
        v1.2: Single model (was dual Gemini+Opus). Configurable via scan_model.
        """
        spec_content = self._read_spec(spec_path)
        if not spec_content:
            return []

        code_tree = await self._get_code_tree()
        prompt = self._build_spec_diff_prompt(spec_content, code_tree)

        verify_cfg = self.config.get("verify", {})
        model = verify_cfg.get("scan_model", "claude-opus-4-6")
        return await self._run_claude_verify(prompt, model)

    def _read_spec(self, spec_path: str) -> str:
        """Read spec file with instance cache (avoids re-reading within a cycle)."""
        if spec_path in self._spec_cache:
            return self._spec_cache[spec_path]
        try:
            with open(spec_path, 'r') as f:
                content = f.read()
            self._spec_cache[spec_path] = content
            return content
        except Exception as e:
            logger.warning(f"[Verifier] Cannot read spec: {e}")
            return ""

    async def _get_code_tree(self) -> str:
        """Get a summary of the code tree (cached for 300s within a cycle)."""
        if self._code_tree_cache and (time.time() - self._code_tree_ts) < 300:
            return self._code_tree_cache

        result = await self._get_code_tree_uncached()
        self._code_tree_cache = result
        self._code_tree_ts = time.time()
        return result

    async def _get_code_tree_uncached(self) -> str:
        """Get a summary of the code tree (file listing + key file contents)."""
        try:
            # Support both TypeScript and Python projects
            proc = await asyncio.create_subprocess_shell(
                'find . -name "*.ts" -o -name "*.tsx" -o -name "*.prisma" -o -name "*.sol" '
                '-o -name "*.py" -o -name "*.sql" '
                '| grep -v node_modules | grep -v .agent-mesh | grep -v typechain '
                '| grep -v __pycache__ | grep -v .pyc | sort',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            file_list = stdout.decode().strip()

            # Read key files (services, routes) up to ~50K chars total
            result_parts = [f"=== File Tree ===\n{file_list}\n"]
            total_chars = len(file_list)
            char_limit = 80_000  # stay within context limits

            for filepath in file_list.split('\n'):
                filepath = filepath.strip()
                if not filepath:
                    continue
                # Prioritize: services, routes, schemas, models, migrations,
                # frontend pages, tests
                if any(kw in filepath for kw in [
                    '/services/', '/routes/', '/schemas/', '/workers/', 'schema.prisma',
                    '_service.py', '/models.py', '/routes.py', '/schemas.py',
                    'migrations/',
                    'page.tsx', 'test_',
                ]):
                    full_path = os.path.join(self.repo_dir, filepath)
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

    def _build_spec_diff_prompt(self, spec: str, code_tree: str) -> str:
        """Build the prompt for spec diff verification."""
        return f"""You are a senior code reviewer verifying a project against its specification.

## Task
Compare the SPECIFICATION below against the ACTUAL CODE and identify gaps.

## Output Format
Respond ONLY with a JSON array of gap objects. No other text.
Each gap object:
{{
  "module": "Module name from spec",
  "requirement": "Specific requirement from spec",
  "status": "NOT_IMPLEMENTED" | "PARTIAL" | "INCORRECT",
  "evidence": "What you see (or don't see) in the code",
  "severity": "HIGH" | "MEDIUM" | "LOW",
  "suggested_fix": "Brief description of what needs to be done"
}}

If everything is implemented correctly, return an empty array: []

## SPECIFICATION
{spec}

## ACTUAL CODE
{code_tree}
"""

    async def _run_claude_verify(self, prompt: str, model: str = "claude-opus-4-6") -> list[VerifyIssue]:
        """Run spec diff with Claude CLI (configurable model). v1.2: replaces dual Gemini+Opus."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        source = model.split("-")[1] if "-" in model else model
        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=900
            )
            return self._parse_gap_json(stdout.decode(), source=source)
        except Exception as e:
            logger.warning(f"[Verifier] Claude verify failed ({model}): {e}")
            return []
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    def _parse_gap_json(self, raw: str, source: str) -> list[VerifyIssue]:
        """Parse JSON gap report from LLM output."""
        # Try to extract JSON array from response
        raw = raw.strip()
        # Remove markdown fences if present
        if '```json' in raw:
            raw = raw.split('```json')[1].split('```')[0].strip()
        elif '```' in raw:
            raw = raw.split('```')[1].split('```')[0].strip()

        try:
            gaps = json.loads(raw)
            if not isinstance(gaps, list):
                gaps = [gaps]
        except json.JSONDecodeError:
            # Try to find array in text
            start = raw.find('[')
            end = raw.rfind(']')
            if start >= 0 and end > start:
                try:
                    gaps = json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    logger.warning(f"[Verifier] Cannot parse {source} gap report")
                    return []
            else:
                return []

        issues = []
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            issues.append(VerifyIssue(
                category="spec_gap",
                severity=gap.get("severity", "MEDIUM"),
                message=f"{gap.get('module', '?')}: {gap.get('requirement', '?')} — {gap.get('status', '?')}",
                module=gap.get("module"),
                found_by=[source],
            ))
        return issues

    def invalidate_caches(self):
        """Clear all caches (call between cycles if needed)."""
        self._code_tree_cache = None
        self._code_tree_ts = 0
        self._spec_cache.clear()
