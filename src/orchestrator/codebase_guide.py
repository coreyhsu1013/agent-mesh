"""
Agent Mesh v1.3 — Codebase Guide Generator

Scans target repo and generates CLAUDE.md with:
1. AI behavior rules
2. Project structure
3. DB schema (from migration files)
4. Service patterns (from existing service/business logic files)
5. Router/controller patterns (from existing route handler files)
6. Test patterns (from test setup + test files)
7. Shared/foundation module interfaces
8. Coding conventions

Paths are configured via config.yaml `codebase_guide` section.
Auto-detects project type and uses smart defaults when not configured.

Config example:
```yaml
codebase_guide:
  # Directories containing shared/foundation code (DO NOT MODIFY rules)
  shared_dirs: ["app/shared"]
  # Glob patterns for service/business logic files
  service_patterns: ["app/*/service.py"]
  # Glob patterns for route handler files
  router_patterns: ["app/*/router.py"]
  # Directories containing DB migrations
  migration_dirs: ["migrations"]
  # Glob patterns for migration files (if not .sql)
  migration_file_patterns: ["*.sql"]
  # Test directory
  test_dir: "tests"
  # Test setup/fixture file
  test_config: "tests/conftest.py"
  # Extra directories to skip during scan
  exclude_dirs: []
```
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent-mesh")

# Max lines to read from each file for pattern extraction
_MAX_LINES_PER_FILE = 120
_MAX_PATTERN_FILES = 3

# Directories always skipped
_ALWAYS_SKIP = {
    ".git", "node_modules", "__pycache__", ".agent-mesh",
    ".next", ".venv", "venv", "dist", "build", ".mypy_cache",
    ".turbo", ".cache", "coverage", ".pytest_cache",
}

# ── Auto-detection presets ──
_PRESETS: dict[str, dict[str, Any]] = {
    "fastapi": {
        "shared_dirs": ["app/shared", "app/core", "src/shared", "src/core"],
        "service_patterns": ["app/*/service.py", "app/*/services.py", "src/*/service.py"],
        "router_patterns": ["app/*/router.py", "app/*/routes.py", "src/*/router.py"],
        "migration_dirs": ["migrations", "alembic/versions"],
        "migration_file_patterns": ["*.sql", "*.py"],
        "test_dir": "tests",
        "test_config": "tests/conftest.py",
    },
    "django": {
        "shared_dirs": ["apps/core", "apps/common", "core"],
        "service_patterns": ["apps/*/services.py", "apps/*/service.py", "*/services.py"],
        "router_patterns": ["apps/*/views.py", "apps/*/api.py", "*/views.py"],
        "migration_dirs": ["*/migrations"],
        "migration_file_patterns": ["*.py"],
        "test_dir": "tests",
        "test_config": "conftest.py",
    },
    "nextjs": {
        "shared_dirs": ["src/lib", "src/utils", "lib", "utils"],
        "service_patterns": ["src/services/*.ts", "src/lib/*.ts", "services/*.ts"],
        "router_patterns": ["src/app/**/route.ts", "src/pages/api/**/*.ts", "pages/api/**/*.ts"],
        "migration_dirs": ["prisma/migrations", "drizzle", "migrations"],
        "migration_file_patterns": ["*.sql", "migration.ts"],
        "test_dir": "__tests__",
        "test_config": "jest.config.ts",
    },
    "nestjs": {
        "shared_dirs": ["src/common", "src/shared", "src/core"],
        "service_patterns": ["src/**/*.service.ts"],
        "router_patterns": ["src/**/*.controller.ts"],
        "migration_dirs": ["src/migrations", "migrations"],
        "migration_file_patterns": ["*.ts"],
        "test_dir": "test",
        "test_config": "test/jest-e2e.json",
    },
    "express": {
        "shared_dirs": ["src/lib", "src/utils", "src/middleware"],
        "service_patterns": ["src/services/*.ts", "src/services/*.js"],
        "router_patterns": ["src/routes/*.ts", "src/routes/*.js", "routes/*.js"],
        "migration_dirs": ["migrations", "db/migrations"],
        "migration_file_patterns": ["*.sql", "*.ts", "*.js"],
        "test_dir": "tests",
        "test_config": "jest.config.js",
    },
}


MAX_GUIDE_BYTES = 30_000  # ~7.5K tokens — keep CLAUDE.md compact for all models


class CodebaseGuide:
    """Generates CLAUDE.md for a target repo to guide AI agents."""

    def __init__(self, config: dict):
        self.config = config
        self.guide_config: dict = config.get("codebase_guide", {})
        self.max_bytes: int = self.guide_config.get("max_bytes", MAX_GUIDE_BYTES)

    async def ensure_guide(self, repo_dir: str, spec_path: str | None = None) -> str | None:
        """
        Generate CLAUDE.md if it doesn't exist or is stale.
        Returns the path to the generated file, or None if skipped.
        """
        claude_md_path = os.path.join(repo_dir, "CLAUDE.md")
        cache_path = os.path.join(repo_dir, ".agent-mesh", "claude-md-cache.json")

        # Resolve scan config: user config > auto-detect > empty
        scan_config = self._resolve_config(repo_dir)

        # Check if we need to regenerate
        current_hash = self._compute_repo_hash(repo_dir, scan_config)
        if os.path.exists(claude_md_path) and os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    cache = json.load(f)
                if cache.get("hash") == current_hash:
                    logger.debug("[CodebaseGuide] CLAUDE.md is up to date, skipping")
                    return claude_md_path
            except Exception:
                pass

        logger.info("[CodebaseGuide] Generating CLAUDE.md for target repo...")
        t0 = time.time()

        # Scan the repo
        scan = self._scan_repo(repo_dir, scan_config)

        # Generate content
        content = self._generate_content(scan)

        # Write CLAUDE.md
        with open(claude_md_path, "w") as f:
            f.write(content)

        # Save cache
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({
                "hash": current_hash,
                "generated_at": time.time(),
                "scan_config": scan_config,
            }, f, indent=2)

        elapsed = time.time() - t0
        logger.info(
            f"[CodebaseGuide] Generated CLAUDE.md ({len(content)} bytes, {elapsed:.1f}s) "
            f"project_type={scan['project_type']}"
        )
        return claude_md_path

    # ── Config resolution ──

    def _resolve_config(self, repo_dir: str) -> dict:
        """
        Resolve scan config: user config > auto-detected preset > empty.
        User config in config.yaml overrides auto-detection.
        """
        # If user specified explicit config, use it
        if self.guide_config:
            resolved = dict(self.guide_config)
            # Detect project type for conventions section
            if "project_type" not in resolved:
                resolved["project_type"] = self._detect_project_type(repo_dir)
            return resolved

        # Auto-detect project type and use preset
        ptype = self._detect_project_type(repo_dir)
        preset = _PRESETS.get(ptype, {})

        # Filter preset paths to only include ones that actually exist
        resolved = {"project_type": ptype}
        for key, paths in preset.items():
            if isinstance(paths, list):
                # For dir-type configs, check which dirs exist
                if key.endswith("_dirs"):
                    existing = [p for p in paths if os.path.isdir(os.path.join(repo_dir, p))]
                    resolved[key] = existing if existing else paths[:1]
                else:
                    # For patterns, keep all (glob matching happens later)
                    resolved[key] = paths
            else:
                resolved[key] = paths

        return resolved

    def _detect_project_type(self, repo_dir: str) -> str:
        """Detect project type from marker files."""
        markers = {
            # Python frameworks
            "fastapi": ["app/main.py", "main.py"],
            "django": ["manage.py", "settings.py"],
            # Node frameworks
            "nextjs": ["next.config.js", "next.config.ts", "next.config.mjs"],
            "nestjs": ["nest-cli.json", "src/main.ts"],
            "express": ["src/app.ts", "src/app.js", "app.js"],
        }

        for ptype, files in markers.items():
            for f in files:
                if os.path.exists(os.path.join(repo_dir, f)):
                    return ptype

        # Fallback: check package.json / requirements.txt
        has_py = os.path.exists(os.path.join(repo_dir, "requirements.txt")) or \
                 os.path.exists(os.path.join(repo_dir, "pyproject.toml"))
        has_node = os.path.exists(os.path.join(repo_dir, "package.json"))

        if has_py and has_node:
            return "fastapi"  # default for mixed
        if has_py:
            return "fastapi"
        if has_node:
            return "express"
        return "unknown"

    # ── Hashing ──

    def _compute_repo_hash(self, repo_dir: str, scan_config: dict) -> str:
        """Hash key files to detect when regeneration is needed."""
        hasher = hashlib.md5()

        # Hash migration dirs
        for mdir in scan_config.get("migration_dirs", []):
            full = os.path.join(repo_dir, mdir)
            if os.path.isdir(full):
                self._hash_dir(hasher, full, repo_dir)

        # Hash shared dirs
        for sdir in scan_config.get("shared_dirs", []):
            full = os.path.join(repo_dir, sdir)
            if os.path.isdir(full):
                self._hash_dir(hasher, full, repo_dir)

        # Hash test config
        tconf = scan_config.get("test_config", "")
        if tconf:
            fpath = os.path.join(repo_dir, tconf)
            if os.path.exists(fpath):
                try:
                    stat = os.stat(fpath)
                    hasher.update(f"{tconf}:{stat.st_mtime}:{stat.st_size}".encode())
                except OSError:
                    pass

        return hasher.hexdigest()

    def _hash_dir(self, hasher, dir_path: str, repo_dir: str):
        """Hash all files in a directory."""
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in _ALWAYS_SKIP]
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, repo_dir)
                try:
                    stat = os.stat(fpath)
                    hasher.update(f"{rel}:{stat.st_mtime}:{stat.st_size}".encode())
                except OSError:
                    pass

    # ── Scanning ──

    def _scan_repo(self, repo_dir: str, scan_config: dict) -> dict[str, Any]:
        """Scan the repo using resolved config."""
        ptype = scan_config.get("project_type", "unknown")
        is_python = ptype in ("fastapi", "django")
        is_node = ptype in ("nextjs", "nestjs", "express")

        scan: dict[str, Any] = {
            "project_type": ptype,
            "structure": self._scan_structure(repo_dir),
            "migrations": self._scan_migrations(repo_dir, scan_config),
            "services": self._scan_by_patterns(
                repo_dir, scan_config.get("service_patterns", []),
                scan_config.get("shared_dirs", []),
            ),
            "routers": self._scan_by_patterns(
                repo_dir, scan_config.get("router_patterns", []),
                scan_config.get("shared_dirs", []),
            ),
            "tests": self._scan_tests(repo_dir, scan_config),
            "shared_modules": self._scan_shared(repo_dir, scan_config),
            "is_python": is_python,
            "is_node": is_node,
        }
        return scan

    def _scan_structure(self, repo_dir: str, max_depth: int = 3) -> list[str]:
        """Scan directory structure up to max_depth."""
        lines = []
        exclude = _ALWAYS_SKIP | set(self.guide_config.get("exclude_dirs", []))

        for root, dirs, files in os.walk(repo_dir):
            dirs[:] = [d for d in sorted(dirs) if d not in exclude]
            depth = root.replace(repo_dir, "").count(os.sep)
            if depth > max_depth:
                dirs.clear()
                continue
            indent = "│   " * depth
            dirname = os.path.basename(root) or os.path.basename(repo_dir)
            if depth > 0:
                lines.append(f"{indent}├── {dirname}/")
            else:
                lines.append(f"{dirname}/")
            # Show code files at this level
            code_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".prisma"}
            key_files = {
                "conftest.py", "main.py", "config.py", "Dockerfile",
                "docker-compose.yml", "package.json", "tsconfig.json",
                "pyproject.toml", "requirements.txt",
            }
            for fname in sorted(files):
                _, ext = os.path.splitext(fname)
                if ext in code_exts or fname in key_files:
                    file_indent = "│   " * (depth + 1)
                    lines.append(f"{file_indent}├── {fname}")
        return lines

    def _scan_migrations(self, repo_dir: str, scan_config: dict) -> list[dict]:
        """Scan migration directories for schema definitions."""
        results = []
        file_pats = scan_config.get("migration_file_patterns", ["*.sql"])

        for mdir in scan_config.get("migration_dirs", []):
            full_dir = os.path.join(repo_dir, mdir)
            if not os.path.isdir(full_dir):
                continue
            for fname in sorted(os.listdir(full_dir)):
                if not any(fnmatch.fnmatch(fname, pat) for pat in file_pats):
                    continue
                fpath = os.path.join(full_dir, fname)
                if os.path.isdir(fpath):
                    continue
                try:
                    with open(fpath) as f:
                        content = f.read()
                except OSError:
                    continue

                # Extract schema (CREATE TABLE for SQL, or model definitions)
                if fname.endswith(".sql"):
                    tables = self._extract_create_tables(content)
                    if tables:
                        results.append({"file": f"{mdir}/{fname}", "tables": tables})
                elif fname.endswith((".ts", ".py")):
                    # For ORM migrations, show the full content (truncated)
                    if len(content) > 3000:
                        content = content[:3000] + "\n// ... (truncated)"
                    results.append({"file": f"{mdir}/{fname}", "tables": [content]})

        return results

    def _scan_by_patterns(
        self, repo_dir: str, patterns: list[str], skip_dirs: list[str],
    ) -> list[dict]:
        """Find files matching glob patterns and extract patterns."""
        results = []
        skip_set = set(os.path.basename(d) for d in skip_dirs) | {"shared", "core", "common"}

        for pattern in patterns:
            # Use pathlib glob for ** support
            for fpath in Path(repo_dir).glob(pattern):
                if not fpath.is_file():
                    continue
                # Skip shared/foundation dirs
                parts = fpath.relative_to(repo_dir).parts
                if any(p in skip_set for p in parts[:-1]):
                    continue
                if any(p in _ALWAYS_SKIP for p in parts):
                    continue

                rel = str(fpath.relative_to(repo_dir))
                module = fpath.parent.name

                # For Python: extract a create/main function
                if fpath.suffix == ".py":
                    func = self._extract_function_pattern(str(fpath), "create")
                    if func:
                        results.append({"module": module, "file": rel, "pattern": func})
                    elif not results:
                        # Fallback: show first N lines
                        content = self._read_head(str(fpath), _MAX_LINES_PER_FILE)
                        if content:
                            results.append({"module": module, "file": rel, "pattern": content})
                else:
                    # TypeScript/JS: show first N lines
                    content = self._read_head(str(fpath), _MAX_LINES_PER_FILE)
                    if content:
                        results.append({"module": module, "file": rel, "pattern": content})

                if len(results) >= _MAX_PATTERN_FILES:
                    return results

        return results

    def _scan_tests(self, repo_dir: str, scan_config: dict) -> dict:
        """Scan test directory and config."""
        result: dict[str, Any] = {}

        # Test config (conftest.py, jest.config, etc.)
        tconf = scan_config.get("test_config", "")
        if tconf:
            fpath = os.path.join(repo_dir, tconf)
            if os.path.exists(fpath):
                try:
                    with open(fpath) as f:
                        result["conftest"] = f.read()[:5000]
                    result["conftest_file"] = tconf
                except OSError:
                    pass

        # First test file
        tdir = scan_config.get("test_dir", "tests")
        tests_dir = os.path.join(repo_dir, tdir)
        if os.path.isdir(tests_dir):
            # Find test files (test_*.py, *.test.ts, *.spec.ts)
            for root, dirs, files in os.walk(tests_dir):
                dirs[:] = [d for d in dirs if d not in _ALWAYS_SKIP]
                for fname in sorted(files):
                    is_test = (
                        fname.startswith("test_") and fname.endswith(".py") or
                        fname.endswith(".test.ts") or fname.endswith(".spec.ts") or
                        fname.endswith(".test.js") or fname.endswith(".spec.js")
                    )
                    if is_test:
                        fpath = os.path.join(root, fname)
                        content = self._read_head(fpath, 80)
                        if content:
                            result["test_example"] = {
                                "file": os.path.relpath(fpath, repo_dir),
                                "content": content,
                            }
                        return result  # Only need first example
        return result

    def _scan_shared(self, repo_dir: str, scan_config: dict) -> list[dict]:
        """Scan shared/foundation modules."""
        results = []
        for sdir in scan_config.get("shared_dirs", []):
            full = os.path.join(repo_dir, sdir)
            if not os.path.isdir(full):
                continue
            for root, dirs, files in os.walk(full):
                dirs[:] = [d for d in dirs if d not in _ALWAYS_SKIP]
                for fname in sorted(files):
                    if fname.startswith("_") and fname != "__init__.py":
                        continue
                    fpath = os.path.join(root, fname)
                    if not fpath.endswith((".py", ".ts", ".js")):
                        continue
                    try:
                        with open(fpath) as f:
                            content = f.read()
                    except OSError:
                        continue

                    functions = self._extract_public_functions(content, fpath)
                    if functions:
                        rel = os.path.relpath(fpath, repo_dir)
                        results.append({"file": rel, "functions": functions})
        return results

    # ── Extraction helpers ──

    def _read_head(self, fpath: str, max_lines: int) -> str | None:
        """Read first N lines of a file."""
        try:
            with open(fpath) as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    lines.append(line.rstrip())
            return "\n".join(lines) if lines else None
        except OSError:
            return None

    def _extract_create_tables(self, sql: str) -> list[str]:
        """Extract CREATE TABLE statements from SQL."""
        tables = []
        lines = sql.split("\n")
        in_create = False
        current: list[str] = []
        paren_depth = 0

        for line in lines:
            upper = line.strip().upper()
            if "CREATE TABLE" in upper:
                in_create = True
                current = [line]
                paren_depth = line.count("(") - line.count(")")
                continue

            if in_create:
                current.append(line)
                paren_depth += line.count("(") - line.count(")")
                if paren_depth <= 0 and ")" in line:
                    tables.append("\n".join(current))
                    in_create = False
                    current = []
                    paren_depth = 0

        return tables

    def _extract_function_pattern(self, fpath: str, keyword: str) -> str | None:
        """Extract the first function containing keyword from a file."""
        try:
            with open(fpath) as f:
                content = f.read()
        except OSError:
            return None

        if fpath.endswith(".py"):
            return self._extract_py_function(content, keyword)
        elif fpath.endswith((".ts", ".js")):
            return self._extract_ts_function(content, keyword)
        return None

    def _extract_py_function(self, content: str, keyword: str) -> str | None:
        """Extract a Python function by keyword."""
        lines = content.split("\n")
        func_start = None
        indent = 0

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if (stripped.startswith(f"async def {keyword}") or
                    stripped.startswith(f"def {keyword}")):
                func_start = i
                indent = len(line) - len(stripped)
                continue

            if func_start is not None and i > func_start + 3:
                if (stripped and not line.startswith(" " * (indent + 1)) and
                        not stripped.startswith("#") and
                        not stripped.startswith('"""') and
                        not stripped.startswith("'''")):
                    if i - func_start > 5:
                        return "\n".join(lines[func_start:i])
                if i - func_start > 60:
                    return "\n".join(lines[func_start:i])

        if func_start is not None:
            end = min(func_start + 60, len(lines))
            return "\n".join(lines[func_start:end])
        return None

    def _extract_ts_function(self, content: str, keyword: str) -> str | None:
        """Extract a TypeScript/JS function by keyword."""
        lines = content.split("\n")
        func_start = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Match: export async function createX, async createX, etc.
            if keyword in stripped.lower() and ("function" in stripped or "=>" in stripped or "async" in stripped):
                func_start = i
                continue

            if func_start is not None and i - func_start > 60:
                return "\n".join(lines[func_start:i])

            # End detection: closing brace at function indent level
            if func_start is not None and i > func_start + 3:
                if stripped == "}" or stripped == "};":
                    return "\n".join(lines[func_start:i + 1])

        if func_start is not None:
            end = min(func_start + 60, len(lines))
            return "\n".join(lines[func_start:end])
        return None

    def _extract_public_functions(self, content: str, fpath: str) -> list[str]:
        """Extract public function signatures."""
        sigs = []
        if fpath.endswith(".py"):
            for line in content.split("\n"):
                stripped = line.strip()
                if (stripped.startswith("async def ") or stripped.startswith("def ")):
                    if stripped.startswith("def _") or stripped.startswith("async def _"):
                        continue
                    sig = stripped.split(":")[0] if ":" in stripped else stripped
                    sigs.append(sig)
        elif fpath.endswith((".ts", ".js")):
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("export ") and ("function" in stripped or "const" in stripped):
                    sig = stripped.split("{")[0].strip() if "{" in stripped else stripped
                    sigs.append(sig)
        return sigs

    # ── Content generation ──

    def _generate_content(self, scan: dict) -> str:
        """Generate CLAUDE.md content from scan results, respecting max_bytes budget."""
        # Priority order: header/overview always included, then most valuable sections first.
        # If budget exceeded, later sections get truncated or dropped.
        required: list[str] = [
            self._section_header(),
            self._section_overview(scan),
        ]
        # Conventions are small and always useful
        required.append(self._section_conventions(scan))

        # Optional sections in priority order (most important first)
        optional: list[tuple[str, str | None]] = []
        if scan["structure"]:
            optional.append(("structure", self._section_structure(scan["structure"])))
        if scan["migrations"]:
            optional.append(("schema", self._section_schema(scan["migrations"])))
        if scan["shared_modules"]:
            optional.append(("shared", self._section_shared_modules(scan["shared_modules"])))
        if scan["services"]:
            optional.append(("services", self._section_patterns(
                "Service Pattern (follow this for new services)", scan["services"],
            )))
        if scan["routers"]:
            optional.append(("routers", self._section_patterns(
                "Router/Controller Pattern (follow this for new routes)", scan["routers"],
            )))
        if scan["tests"]:
            optional.append(("tests", self._section_test_pattern(scan["tests"])))

        # Build content within budget
        sections = list(required)
        used = sum(len(s.encode()) for s in sections) + len(sections) * 2  # \n\n separators
        budget = self.max_bytes

        for label, section in optional:
            section_bytes = len(section.encode())
            if used + section_bytes + 2 <= budget:
                sections.insert(-1, section)  # insert before conventions (last required)
                used += section_bytes + 2
            else:
                # Try to fit a truncated version (keep first 40% of budget remainder)
                remaining = budget - used - 2
                if remaining > 500:
                    truncated = self._truncate_section(section, remaining)
                    sections.insert(-1, truncated)
                    used += len(truncated.encode()) + 2
                    logger.info(
                        f"[CodebaseGuide] Section '{label}' truncated "
                        f"({section_bytes}→{len(truncated.encode())} bytes)"
                    )
                else:
                    logger.info(
                        f"[CodebaseGuide] Section '{label}' dropped "
                        f"({section_bytes} bytes, budget={budget}, used={used})"
                    )
                break  # stop adding sections once budget is tight

        content = "\n\n".join(sections) + "\n"
        logger.info(
            f"[CodebaseGuide] Content size: {len(content.encode())} bytes "
            f"(limit: {budget} bytes, {len(content.encode()) * 100 // budget}%)"
        )
        return content

    @staticmethod
    def _truncate_section(section: str, max_bytes: int) -> str:
        """Truncate a section to fit within max_bytes, cutting at line boundaries."""
        lines = section.split("\n")
        result: list[str] = []
        size = 0
        for line in lines:
            line_bytes = len(line.encode()) + 1  # +1 for \n
            if size + line_bytes > max_bytes - 30:  # reserve space for truncation note
                break
            result.append(line)
            size += line_bytes
        result.append("\n<!-- ... truncated to fit budget -->")
        return "\n".join(result)

    def _section_header(self) -> str:
        return """# CLAUDE.md — AI Development Guide

## AI Behavior Rules
- Do NOT use the Agent tool to explore the codebase
- Do NOT read more than 5 files before starting to write code
- This file contains all the architecture info you need — start implementing immediately
- Follow the existing patterns shown below as reference
- NEVER modify files in shared/foundation modules without explicit instruction"""

    def _section_overview(self, scan: dict) -> str:
        ptype = scan["project_type"]
        type_desc = {
            "fastapi": "Python FastAPI",
            "django": "Python Django",
            "nextjs": "Next.js (TypeScript)",
            "nestjs": "NestJS (TypeScript)",
            "express": "Express.js",
        }.get(ptype, ptype)
        return f"## Project Overview\nProject type: **{type_desc}**"

    def _section_structure(self, structure: list[str]) -> str:
        tree = "\n".join(structure[:80])
        return f"## Directory Structure\n```\n{tree}\n```"

    def _section_schema(self, migrations: list[dict]) -> str:
        parts = ["## Database Schema\n"]
        for mig in migrations:
            parts.append(f"### From `{mig['file']}`")
            for table_sql in mig["tables"][:5]:
                if len(table_sql) > 2000:
                    table_sql = table_sql[:2000] + "\n-- ... (truncated)"
                lang = "sql" if mig["file"].endswith(".sql") else "typescript"
                parts.append(f"```{lang}\n{table_sql}\n```")
        return "\n".join(parts)

    def _section_shared_modules(self, modules: list[dict]) -> str:
        parts = ["## Shared Module Interfaces (DO NOT MODIFY, just use)\n"]
        for mod in modules:
            lang = "python" if mod["file"].endswith(".py") else "typescript"
            parts.append(f"### `{mod['file']}`")
            parts.append(f"```{lang}")
            for sig in mod["functions"][:10]:
                parts.append(sig)
            parts.append("```")
        return "\n".join(parts)

    def _section_patterns(self, title: str, items: list[dict]) -> str:
        parts = [f"## {title}\n"]
        for item in items[:1]:  # Only first example
            lang = "python" if item["file"].endswith(".py") else "typescript"
            # Trim pattern to reasonable size
            pattern = item["pattern"]
            lines = pattern.split("\n")
            if len(lines) > 60:
                pattern = "\n".join(lines[:60]) + "\n// ... (truncated)"
            parts.append(f"From `{item['file']}` ({item['module']} module):")
            parts.append(f"```{lang}\n{pattern}\n```")
        return "\n".join(parts)

    def _section_test_pattern(self, tests: dict) -> str:
        parts = ["## Test Pattern\n"]
        if "test_example" in tests:
            ex = tests["test_example"]
            lang = "python" if ex["file"].endswith(".py") else "typescript"
            lines = ex["content"].split("\n")[:50]
            parts.append(f"From `{ex['file']}`:")
            parts.append(f"```{lang}\n" + "\n".join(lines) + "\n```")

        if "conftest" in tests:
            parts.append(f"\n**Test setup from `{tests.get('conftest_file', 'conftest')}`:**")
            conftest = tests["conftest"]
            # Extract fixture/setup function names
            fixtures = []
            for line in conftest.split("\n"):
                stripped = line.strip()
                if stripped.startswith(("async def ", "def ")) and "fixture" not in stripped:
                    fname = stripped.split("(")[0].replace("async def ", "").replace("def ", "")
                    if not fname.startswith("_"):
                        fixtures.append(fname)
            if fixtures:
                parts.append("Available fixtures: `" + "`, `".join(fixtures) + "`")

        return "\n".join(parts)

    def _section_conventions(self, scan: dict) -> str:
        ptype = scan["project_type"]
        if ptype in ("fastapi", "django"):
            return """## Coding Conventions
- Primary keys: UUID
- Timestamps: TIMESTAMPTZ
- Monetary values: NUMERIC(12,2)
- asyncpg placeholders: `$1, $2, $3` (NOT `%s` or `?`)
- Row results from `conn.fetchrow()` are dict-like: `row["column_name"]`
- All state changes go through Event Store (if applicable)
- Business config values in system_config, read with `get_config_value()`"""
        elif ptype in ("nextjs", "nestjs", "express"):
            return """## Coding Conventions
- Use TypeScript for all new files
- Follow existing import style (relative vs absolute)
- Use proper type annotations (avoid `any`)
- Run build command to check for type errors
- Follow existing error handling patterns"""
        return "## Coding Conventions\nFollow existing patterns in the codebase."
