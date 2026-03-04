"""
Agent Mesh v0.9 — Project Classifier

Auto-detect project type from repo file patterns.
Supports manual override via .agent-mesh/project.yaml.

Project types:
  web       — Web apps (React, Next.js, Vue, etc.)
  erp       — ERP/business systems (Django, FastAPI, Prisma, etc.)
  embedded  — Embedded systems (C/C++, CMake, PlatformIO)
  iot       — IoT devices (MQTT, sensors, firmware)
  chip      — Chip/FPGA design (Verilog, VHDL, SystemVerilog)
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path

logger = logging.getLogger("agent-mesh")

# File patterns → project type scoring
TYPE_PATTERNS: dict[str, list[str]] = {
    "web": [
        "package.json", "next.config.js", "next.config.ts", "next.config.mjs",
        "vite.config.ts", "vite.config.js", "nuxt.config.ts",
        "angular.json", "svelte.config.js",
    ],
    "erp": [
        "manage.py", "alembic.ini", "alembic/",
    ],
    "embedded": [
        "CMakeLists.txt", "platformio.ini", "Makefile",
        "STM32", "stm32", "arduino",
    ],
    "iot": [
        "mqtt", "firmware/", "sensor",
    ],
    "chip": [
        "synthesis/", "rtl/", "testbench/",
    ],
}

# File extensions → project type scoring
TYPE_EXTENSIONS: dict[str, list[str]] = {
    "web": [".tsx", ".jsx", ".vue", ".svelte"],
    "erp": [".py"],
    "embedded": [".c", ".h", ".cpp", ".hpp", ".ino"],
    "iot": [".c", ".h", ".py"],
    "chip": [".v", ".vhd", ".sv", ".vhdl"],
}

# Extension → language mapping
EXTENSION_LANGUAGE: dict[str, str] = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".py": "python",
    ".c": "c", ".h": "c", ".cpp": "c++", ".hpp": "c++",
    ".v": "verilog", ".sv": "systemverilog", ".vhd": "vhdl",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".swift": "swift", ".rb": "ruby", ".php": "php",
}

# Config files → framework mapping
FRAMEWORK_INDICATORS: dict[str, str] = {
    "next.config": "nextjs",
    "nuxt.config": "nuxt",
    "vite.config": "vite",
    "angular.json": "angular",
    "svelte.config": "svelte",
    "remix.config": "remix",
    "astro.config": "astro",
    "manage.py": "django",
    "alembic.ini": "sqlalchemy",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "platformio.ini": "platformio",
    "CMakeLists.txt": "cmake",
}


class ProjectClassifier:
    """Auto-detect project type, language, and framework from repo."""

    def classify(self, repo_path: str) -> dict:
        """
        Classify a project. Returns dict with:
          project_type, language, framework
        Checks .agent-mesh/project.yaml for manual override first.
        """
        # Check for manual override
        override = self._load_override(repo_path)
        if override:
            logger.info(f"[Classifier] Manual override: {override}")
            return {
                "project_type": override.get("project_type", self._detect_type(repo_path)),
                "language": override.get("language", self._detect_language(repo_path)),
                "framework": override.get("framework", self._detect_framework(repo_path)),
            }

        project_type = self._detect_type(repo_path)
        language = self._detect_language(repo_path)
        framework = self._detect_framework(repo_path)

        logger.info(
            f"[Classifier] {repo_path} → type={project_type}, "
            f"lang={language}, framework={framework}"
        )

        return {
            "project_type": project_type,
            "language": language,
            "framework": framework,
        }

    def _load_override(self, repo_path: str) -> dict | None:
        """Load manual override from .agent-mesh/project.yaml."""
        yaml_path = os.path.join(repo_path, ".agent-mesh", "project.yaml")
        if not os.path.exists(yaml_path):
            return None
        try:
            import yaml
            with open(yaml_path) as f:
                data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _detect_type(self, repo_path: str) -> str:
        """Score each project type by file pattern matches."""
        scores: dict[str, int] = {t: 0 for t in TYPE_PATTERNS}

        # Score by file/directory name patterns
        for project_type, patterns in TYPE_PATTERNS.items():
            for pattern in patterns:
                if pattern.endswith("/"):
                    # Directory check
                    if os.path.isdir(os.path.join(repo_path, pattern.rstrip("/"))):
                        scores[project_type] += 3
                else:
                    # File check (also check subdirs one level deep)
                    if os.path.exists(os.path.join(repo_path, pattern)):
                        scores[project_type] += 2

        # Score by file extensions (sample top-level and one level deep)
        ext_counts = self._count_extensions(repo_path, max_depth=2)
        for project_type, extensions in TYPE_EXTENSIONS.items():
            for ext in extensions:
                scores[project_type] += ext_counts.get(ext, 0) // 5  # 5 files = 1 point

        # Special heuristics
        # prisma → could be web or erp; check for admin/models patterns
        if os.path.exists(os.path.join(repo_path, "prisma")) or \
           os.path.exists(os.path.join(repo_path, "packages/database/prisma")):
            scores["web"] += 2
            scores["erp"] += 1

        if not any(v > 0 for v in scores.values()):
            return "web"  # default

        return max(scores, key=lambda k: scores[k])

    def _detect_language(self, repo_path: str) -> str:
        """Detect dominant programming language."""
        ext_counts = self._count_extensions(repo_path, max_depth=3)

        lang_counts: Counter = Counter()
        for ext, count in ext_counts.items():
            lang = EXTENSION_LANGUAGE.get(ext)
            if lang:
                lang_counts[lang] += count

        if not lang_counts:
            return "unknown"
        return lang_counts.most_common(1)[0][0]

    def _detect_framework(self, repo_path: str) -> str:
        """Detect framework from config files."""
        for indicator, framework in FRAMEWORK_INDICATORS.items():
            # Check root
            if os.path.exists(os.path.join(repo_path, indicator)):
                return framework
            # Check one level deep (monorepo apps/)
            apps_dir = os.path.join(repo_path, "apps")
            if os.path.isdir(apps_dir):
                for entry in os.listdir(apps_dir):
                    if os.path.exists(os.path.join(apps_dir, entry, indicator)):
                        return framework
        return "unknown"

    def _count_extensions(self, repo_path: str, max_depth: int = 2) -> dict[str, int]:
        """Count file extensions up to max_depth levels."""
        counts: Counter = Counter()
        skip_dirs = {"node_modules", ".git", "__pycache__", ".next", "dist", "build", ".agent-mesh"}

        for root, dirs, files in os.walk(repo_path):
            # Calculate depth
            depth = root.replace(repo_path, "").count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            # Skip noisy directories
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext:
                    counts[ext] += 1

        return dict(counts)
