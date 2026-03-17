"""
Agent Mesh v2.0 — Basic Deterministic Checks
First-version gate check implementations.

Principles:
1. Minimal viable — don't over-engineer
2. Repo-generic — works across project types
3. Conservative on unconfigured projects — don't false-positive
4. Reuse existing build/test behavior where possible
"""

from __future__ import annotations
import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# Input Checks — validate task inputs before execution
# ══════════════════════════════════════════════════════════

def target_files_defined(task, **kwargs) -> tuple[bool, str]:
    """Check that task has at least one target file defined."""
    files = getattr(task, "target_files", None) or []
    if files:
        return True, f"{len(files)} target files defined"
    return False, "No target_files defined on task"


def acceptance_defined(task, **kwargs) -> tuple[bool, str]:
    """Check that task has acceptance criteria."""
    criteria = getattr(task, "acceptance_criteria", "") or ""
    if criteria.strip():
        return True, "Acceptance criteria defined"
    return False, "No acceptance_criteria defined on task"


# ══════════════════════════════════════════════════════════
# Rule Checks — deterministic rule enforcement on diff
# ══════════════════════════════════════════════════════════

# Build artifact directories — no task should ever commit these.
_BUILD_ARTIFACT_DIRS = {
    ".next", "dist", "build", "out", "node_modules",
    "__pycache__", ".turbo", ".cache",
}

# Root-level monorepo config files — need explicit target_files permission.
_MONOREPO_PROTECTED = {
    "pnpm-workspace.yaml", "workspace.yaml",
    "turbo.json", "nx.json", "lerna.json",
    "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "package-lock.json",
    "package.json",
}


def no_build_artifacts(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """
    Block build artifacts from being committed.
    Catches .next/, dist/, node_modules/, etc. anywhere in the tree.
    Always enforced — no opt-out.
    """
    if not diff:
        return True, "No diff to check"

    changed = _extract_changed_files(diff)
    violations = [
        f for f in changed
        if any(part in _BUILD_ARTIFACT_DIRS for part in f.split("/"))
    ]

    if violations:
        return False, (
            f"Build artifacts committed (blocked): "
            f"{', '.join(violations[:5])}"
            + (f" (+{len(violations) - 5} more)" if len(violations) > 5 else "")
        )
    return True, "No build artifacts in diff"


def no_monorepo_config(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """
    Block modifications to root-level monorepo config files
    unless explicitly listed in task target_files.
    Protects: pnpm-workspace.yaml, turbo.json, lockfiles, root package.json.
    """
    if not diff:
        return True, "No diff to check"

    changed = _extract_changed_files(diff)
    target_files = set(getattr(task, "target_files", None) or [])

    violations = [
        f for f in changed
        if f in _MONOREPO_PROTECTED and f not in target_files
    ]

    if violations:
        return False, (
            f"Monorepo config modified without permission: "
            f"{', '.join(violations)}"
        )
    return True, "No unauthorized monorepo config changes"


def no_runtime_modification(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """
    Block modifications to orchestrator runtime/control-plane files.
    Protects: .agent-mesh/ (logs, db, state, events, workspace metadata).
    Always enforced — no opt-out.
    """
    if not diff:
        return True, "No diff to check"

    changed = _extract_changed_files(diff)
    violations = [f for f in changed if f.startswith(".agent-mesh/")]

    if violations:
        return False, (
            f"Runtime files modified (blocked): "
            f"{', '.join(violations[:5])}"
        )
    return True, "No runtime files touched"


# Companion directories that backend feature tasks commonly need
# beyond their own module directory.
_FEATURE_COMPANION_DIRS = {
    "prisma", "drizzle", "typeorm", "sequelize", "db", "database",
    "test", "tests", "__tests__", "spec", "e2e",
    "config", "configs", "scripts", "migrations", "schemas", "graphql",
}

# Boundary markers for app root detection (monorepo)
_APP_BOUNDARY_MARKERS = {"apps", "packages", "services", "libs"}


def _is_backend_feature_task(task) -> bool:
    """Detect if a task is a backend feature task (CRUD, service, module, etc.)."""
    category = (getattr(task, "category", "") or "").lower()
    if category not in ("backend", "fullstack"):
        return False
    task_type = (getattr(task, "task_type", "") or "").lower()
    title = (getattr(task, "title", "") or "").lower()
    # Feature indicators in task_type or title
    feature_keywords = {
        "crud", "service", "module", "controller", "resolver",
        "endpoint", "api", "resource", "handler",
    }
    combined = f"{task_type} {title}"
    return any(kw in combined for kw in feature_keywords)


def _find_app_root(dir_path: str) -> str:
    """
    Find the app boundary directory from a path.
    - "apps/api/src/modules/products" → "apps/api"
    - "packages/shared/src/types"     → "packages/shared"
    - "src/modules/products"          → "" (flat project, repo root)
    """
    parts = dir_path.split("/")
    for i, part in enumerate(parts):
        if part in _APP_BOUNDARY_MARKERS and i + 1 < len(parts):
            return "/".join(parts[:i + 2])
    return ""


def _expand_for_feature_slice(task, allowed_dirs: set[str]) -> set[str]:
    """
    For backend feature tasks, add companion directories (prisma, test, config, etc.)
    under the same app root.
    Returns the set of additional allowed directories (empty if not applicable).
    """
    if not _is_backend_feature_task(task):
        return set()

    expanded = set()
    # Collect unique app roots from existing allowed dirs
    app_roots = set()
    for d in allowed_dirs:
        if not d:
            continue
        root = _find_app_root(d)
        app_roots.add(root)

    for root in app_roots:
        prefix = (root + "/") if root else ""
        for companion in _FEATURE_COMPANION_DIRS:
            expanded.add(f"{prefix}{companion}")

    return expanded


def allowed_paths_only(task, diff: str = "", workspace_dir: str = "", **kwargs) -> tuple[bool, str]:
    """
    Check that changed files are within expected paths.
    Conservative: if no target_files defined, pass (don't block).

    For backend feature tasks, companion directories (prisma, test, config, etc.)
    under the same app root are automatically allowed.
    """
    if not diff:
        return True, "No diff to check"

    target_files = getattr(task, "target_files", None) or []
    if not target_files:
        return True, "No target_files constraint — skipping path check"

    # Extract changed files from diff
    changed = _extract_changed_files(diff)
    if not changed:
        return True, "No files changed"

    # Build allowed directories from target_files
    allowed_dirs = set()
    for tf in target_files:
        # Allow the file itself and its parent directory
        allowed_dirs.add(os.path.dirname(tf))
        allowed_dirs.add(tf)

    # Expand for backend feature tasks (prisma, test, config, etc.)
    expanded = _expand_for_feature_slice(task, allowed_dirs)
    all_allowed = allowed_dirs | expanded

    # Check each changed file
    violations = []
    for f in changed:
        # Check if file or its parent is in allowed set
        in_allowed = False
        for allowed in all_allowed:
            if not allowed:  # root dir
                in_allowed = True
                break
            if f == allowed or f.startswith(allowed + "/") or allowed.startswith(os.path.dirname(f)):
                in_allowed = True
                break
        if not in_allowed:
            violations.append(f)

    if violations:
        return False, f"Files outside target paths: {', '.join(violations[:5])}"
    suffix = f" (+{len(expanded)} companion dirs)" if expanded else ""
    return True, f"{len(changed)} files all within allowed paths{suffix}"


def no_new_dependency(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """
    Check that no new package dependencies were added.
    Only flags — not a hard blocker for most profiles.
    Conservative: only checks package.json and requirements.txt patterns.
    """
    if not diff:
        return True, "No diff to check"

    # Look for additions to dependency sections
    suspicious_patterns = [
        r'^\+\s*"[^"]+"\s*:\s*"[\^~]?\d',  # package.json dep addition
        r'^\+[a-zA-Z].*==',                  # requirements.txt addition
    ]

    lines = diff.split("\n")
    new_deps = []
    for line in lines:
        for pattern in suspicious_patterns:
            if re.search(pattern, line):
                new_deps.append(line.strip()[:80])
                break

    if new_deps:
        return False, f"New dependencies detected: {'; '.join(new_deps[:3])}"
    return True, "No new dependencies detected"


def no_secret_leak(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """
    Check for potential secret/credential leaks in diff.
    Conservative: only flags obvious patterns.
    """
    if not diff:
        return True, "No diff to check"

    secret_patterns = [
        (r'(?i)(api[_-]?key|secret[_-]?key|password|token)\s*[=:]\s*["\'][a-zA-Z0-9]{16,}', "Hardcoded secret"),
        (r'sk-[a-zA-Z0-9]{20,}', "OpenAI-style API key"),
        (r'ghp_[a-zA-Z0-9]{36,}', "GitHub personal access token"),
        (r'-----BEGIN (RSA |EC )?PRIVATE KEY-----', "Private key"),
    ]

    # Only check added lines
    added_lines = [
        line[1:] for line in diff.split("\n")
        if line.startswith("+") and not line.startswith("+++")
    ]
    added_text = "\n".join(added_lines)

    findings = []
    for pattern, label in secret_patterns:
        if re.search(pattern, added_text):
            findings.append(label)

    if findings:
        return False, f"Potential secret leak: {', '.join(findings)}"
    return True, "No secret patterns detected"


# ══════════════════════════════════════════════════════════
# Verification Checks — post-execution verification
# ══════════════════════════════════════════════════════════

def diff_not_empty(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """Check that the task actually produced changes."""
    if diff and diff.strip() and len(diff.strip()) > 10:
        lines = len(diff.split("\n"))
        return True, f"Diff has {lines} lines"
    return False, "Diff is empty — task produced no changes"


def dod_diff_required(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """Analysis task skip; implementation task must have diff."""
    if getattr(task, "allowed_no_diff", False):
        return True, "Analysis task — diff not required"
    if not diff or len(diff.strip()) <= 10:
        return False, "Implementation task produced no diff"
    return True, ""


def dod_must_change_files(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """Must change at least one file from required_target_files."""
    must = getattr(task, "required_target_files", [])
    if not must:
        return True, ""
    changed = _extract_changed_files(diff)
    for mf in must:
        if any(mf in c or c.endswith(mf) for c in changed):
            return True, f"Required file changed: {mf}"
    return False, f"None of required_target_files in diff: {must[:3]}"


async def build_pass(task, workspace_dir: str = "", **kwargs) -> tuple[bool, str]:
    """
    Run build check in workspace.
    Strategy:
    1. Detect package manager from lock file (pnpm > yarn > bun > npm)
    2. Determine build command: package.json "build" script > tsconfig.json tsc > skip
    3. Run exactly ONE command — no || fallthrough that masks errors
    Conservative: if no build mechanism found, pass.
    """
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return True, "No workspace dir — skipping build check"

    pm = _detect_package_manager(workspace_dir)
    build_cmd = _resolve_build_cmd(workspace_dir, pm)

    if not build_cmd:
        return True, "No build script or tsconfig found — skipping"

    build_output = await _run_cmd(f"cd {workspace_dir} && {build_cmd} 2>&1", timeout=120)

    if _has_build_errors(build_output):
        return False, f"Build failed ({pm}): {build_output[:300]}"
    return True, f"Build passed ({pm})"


async def tests_pass(task, workspace_dir: str = "", **kwargs) -> tuple[bool, str]:
    """
    Run tests in workspace.
    Strategy:
    1. Detect package manager from lock file
    2. Detect test framework: vitest > jest > generic "test" script
    3. Run exactly ONE test command with appropriate flags
    Conservative: if no test script found, pass.
    """
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return True, "No workspace dir — skipping test check"

    pm = _detect_package_manager(workspace_dir)
    test_cmd = _resolve_test_cmd(workspace_dir, pm)

    if not test_cmd:
        return True, "No test script found — skipping"

    test_output = await _run_cmd(f"cd {workspace_dir} && {test_cmd} 2>&1", timeout=120)

    if _has_test_failures(test_output):
        return False, f"Tests failed ({pm}): {test_output[:300]}"
    return True, f"Tests passed ({pm})"


# ══════════════════════════════════════════════════════════
# Escalation Checks — flag for human/senior review
# (These return (should_escalate, reason) — True = needs escalation)
# ══════════════════════════════════════════════════════════

def auth_or_payment_touched(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """Flag if auth or payment related files were modified."""
    if not diff:
        return False, ""

    changed = _extract_changed_files(diff)
    sensitive_patterns = ["auth", "payment", "billing", "stripe", "session", "token", "credential"]

    touched = []
    for f in changed:
        f_lower = f.lower()
        for pattern in sensitive_patterns:
            if pattern in f_lower:
                touched.append(f)
                break

    if touched:
        return True, f"Sensitive files touched: {', '.join(touched[:5])}"
    return False, "No sensitive files touched"


def migration_detected(task, diff: str = "", **kwargs) -> tuple[bool, str]:
    """Flag if database migration files were created/modified."""
    if not diff:
        return False, ""

    changed = _extract_changed_files(diff)
    migration_patterns = ["migration", "migrate", "prisma/migrations", "alembic", "knex"]

    migrations = []
    for f in changed:
        f_lower = f.lower()
        for pattern in migration_patterns:
            if pattern in f_lower:
                migrations.append(f)
                break

    if migrations:
        return True, f"Migration files detected: {', '.join(migrations[:5])}"
    return False, "No migration files detected"


# ══════════════════════════════════════════════════════════
# Registry — maps check name to function
# ══════════════════════════════════════════════════════════

CHECK_REGISTRY: dict[str, callable] = {
    # Input checks
    "target_files_defined": target_files_defined,
    "acceptance_defined": acceptance_defined,
    # Rule checks
    "no_runtime_modification": no_runtime_modification,
    "no_build_artifacts": no_build_artifacts,
    "no_monorepo_config": no_monorepo_config,
    "allowed_paths_only": allowed_paths_only,
    "no_new_dependency": no_new_dependency,
    "no_secret_leak": no_secret_leak,
    # Verification checks
    "diff_not_empty": diff_not_empty,
    "dod_diff_required": dod_diff_required,
    "dod_must_change_files": dod_must_change_files,
    "build_pass": build_pass,
    "tests_pass": tests_pass,
    # Escalation checks
    "auth_or_payment_touched": auth_or_payment_touched,
    "migration_detected": migration_detected,
}


# ══════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════

def _extract_changed_files(diff: str) -> list[str]:
    """Extract file paths from git diff output."""
    files = []
    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) > 1:
                files.append(parts[1])
    return files


def _detect_package_manager(workspace_dir: str) -> str:
    """
    Detect package manager from lock file.
    Priority: pnpm > yarn > bun > npm (fallback).
    """
    if os.path.exists(os.path.join(workspace_dir, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(workspace_dir, "yarn.lock")):
        return "yarn"
    if os.path.exists(os.path.join(workspace_dir, "bun.lockb")):
        return "bun"
    return "npm"


def _read_package_json(workspace_dir: str) -> dict:
    """Read and parse package.json, return empty dict on failure."""
    pkg_path = os.path.join(workspace_dir, "package.json")
    if not os.path.exists(pkg_path):
        return {}
    try:
        import json
        with open(pkg_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _resolve_build_cmd(workspace_dir: str, pm: str) -> str | None:
    """
    Determine the single best build command for the workspace.
    Returns None if no build mechanism is found.

    Resolution order:
    1. package.json has "build" script → {pm} run build
    2. tsconfig.json exists → npx tsc --noEmit
    3. None (no build — gate will skip)
    """
    pkg = _read_package_json(workspace_dir)
    scripts = pkg.get("scripts", {})

    if "build" in scripts:
        return f"{pm} run build"

    if os.path.exists(os.path.join(workspace_dir, "tsconfig.json")):
        return "npx tsc --noEmit"

    return None


def _resolve_test_cmd(workspace_dir: str, pm: str) -> str | None:
    """
    Determine the single best test command for the workspace.
    Returns None if no test mechanism is found.

    Resolution order:
    1. vitest in devDependencies → {pm} test --passWithNoTests
    2. jest in devDependencies   → {pm} test -- --passWithNoTests
    3. "test" script exists      → {pm} test
    4. None (no tests — gate will skip)
    """
    pkg = _read_package_json(workspace_dir)
    all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts", {})

    if "vitest" in all_deps:
        return f"{pm} test --passWithNoTests"

    if "jest" in all_deps:
        return f"{pm} test -- --passWithNoTests"

    if "test" in scripts:
        # Generic test script — don't add framework-specific flags
        return f"{pm} test"

    return None


def _has_build_errors(output: str) -> bool:
    """Check for build error indicators. Uses exit code aware patterns."""
    if not output or output.strip() == "NO_BUILD_SCRIPT":
        return False
    # Specific patterns that reliably indicate build failure
    indicators = [
        "error TS",                    # TypeScript compiler error
        "SyntaxError:",                # JS/TS syntax error
        "TypeError:",                  # Type error at build time
        "ReferenceError:",             # Reference error at build time
        "Cannot find module",          # Missing module
        "Module not found",            # Webpack/Next.js missing module
        "failed with exit code",       # Process exit with error
        "Build error",                 # Next.js build error
        "ENOENT",                      # File not found
        "ERR!",                        # npm/pnpm error marker
    ]
    return any(ind in output for ind in indicators)


def _has_test_failures(output: str) -> bool:
    """Check for test failure indicators."""
    if not output or output.strip() == "NO_TEST_SCRIPT":
        return False
    indicators = [
        "FAIL ",                       # Jest/vitest FAIL marker (with space to avoid "FAILED")
        "Tests:.*failed",              # Jest summary
        "Test Files.*failed",          # Vitest summary
        " failing",                    # Mocha style
        "AssertionError",              # Assertion failure
        "failed with exit code",       # Process exit with error
        "ERR!",                        # npm/pnpm error marker
    ]
    return any(re.search(ind, output) for ind in indicators)


async def _run_cmd(cmd: str, timeout: int = 60) -> str:
    """Run shell command with timeout."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace")[:5000] if stdout else ""
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"CMD_ERROR: {e}"
