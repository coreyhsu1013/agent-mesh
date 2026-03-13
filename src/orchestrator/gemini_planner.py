"""
Agent Mesh v0.7.1 — Gemini Planner (Two-Phase)

Two-phase planning:
  Phase 1 — Task Classification (fast model, e.g. Gemini Flash)
    Read spec → identify tasks → assign category (backend/frontend/fullstack)
  Phase 2 — Detail Planning (specialized models, parallel)
    backend tasks   → config.planner.backend model
    frontend/fullstack → config.planner.frontend model
    failure         → config.planner.fallback model

Fallback: if classification fails, revert to single-phase planning (original v0.6 flow).
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import shlex
import tempfile
from typing import Optional

from src.auth.claude_account_pool import get_pool

logger = logging.getLogger(__name__)


class PlannerError(Exception):
    pass


class GeminiPlanner:
    """
    Two-phase planner: classify → detail.

    Phase 1 (classify model): fast classification of tasks from spec.
    Phase 2 (backend/frontend models): detailed planning per category.
    """

    def __init__(self, config: dict):
        planner_cfg = config.get("planner", {})
        self.provider = planner_cfg.get("provider", "gemini")
        self.model = planner_cfg.get("model", "claude-opus-4-6")
        self.model_fallback = planner_cfg.get("fallback", self.model)
        # Legacy compat: two-phase fields (unused, kept for config backward compat)
        self.model_classify = planner_cfg.get("classify", self.model)
        self.model_backend = planner_cfg.get("backend", self.model)
        self.model_frontend = planner_cfg.get("frontend", self.model)
        self.timeout = planner_cfg.get("timeout", 300)

    async def plan(
        self,
        spec_content: str,
        agents_md: str = "",
        project_name: str = "project",
        is_canonical: bool = False,
    ) -> dict:
        """
        Single-phase planning: spec → full plan in one shot.
        is_canonical=True when spec was pre-filtered by SpecOS (planning-spec.md).
        """
        return await self._plan_single_phase(
            spec_content, agents_md, project_name, is_canonical,
        )

    # ══════════════════════════════════════════════════════════
    # Two-Phase Planning
    # ══════════════════════════════════════════════════════════

    async def _plan_two_phase(
        self,
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> dict:
        """Phase 1 classify → Phase 2 detail (parallel) → merge."""

        # ── Phase 1: Classification ──
        logger.info(
            f"[GeminiPlanner] Phase 1: classifying tasks with {self.model_classify}"
            f"{' (canonical spec)' if is_canonical else ''}"
        )
        classify_prompt = self._build_classify_prompt(
            spec_content, agents_md, project_name, is_canonical,
        )
        raw = await self._call_model(self.model_classify, classify_prompt)
        skeleton = self._parse_plan(raw)

        tasks = skeleton.get("tasks", [])
        if not tasks:
            raise PlannerError("Phase 1 produced no tasks")

        # Split by category
        backend_tasks = [t for t in tasks if t.get("category") == "backend"]
        frontend_tasks = [
            t for t in tasks if t.get("category") in ("frontend", "fullstack")
        ]
        # Uncategorized → treat as backend
        uncategorized = [
            t for t in tasks
            if t.get("category") not in ("backend", "frontend", "fullstack")
        ]
        backend_tasks.extend(uncategorized)

        logger.info(
            f"[GeminiPlanner] Phase 1 done: "
            f"{len(backend_tasks)} backend, {len(frontend_tasks)} frontend/fullstack"
        )

        # Save id→category mapping (in case Phase 2 drops it)
        category_map = {t["id"]: t.get("category", "backend") for t in tasks}

        # ── Phase 2: Detail Planning (parallel) ──
        coros = []
        if backend_tasks:
            coros.append(
                self._detail_with_fallback(
                    self.model_backend, backend_tasks,
                    spec_content, agents_md, project_name, "backend",
                    is_canonical,
                )
            )
        if frontend_tasks:
            coros.append(
                self._detail_with_fallback(
                    self.model_frontend, frontend_tasks,
                    spec_content, agents_md, project_name, "frontend",
                    is_canonical,
                )
            )

        detailed_tasks: list[dict] = []
        if coros:
            results = await asyncio.gather(*coros, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    label = "backend" if i == 0 and backend_tasks else "frontend"
                    logger.error(
                        f"[GeminiPlanner] Phase 2 {label} failed: {result}, "
                        f"using skeleton tasks"
                    )
                    # Use skeleton tasks as fallback
                    group = backend_tasks if label == "backend" else frontend_tasks
                    detailed_tasks.extend(group)
                else:
                    detailed_tasks.extend(result)

        # Restore category from Phase 1 (in case Phase 2 dropped it)
        for t in detailed_tasks:
            if not t.get("category") and t.get("id") in category_map:
                t["category"] = category_map[t["id"]]

        # ── Merge ──
        plan = {
            "project_name": skeleton.get("project_name", project_name),
            "shared_context": skeleton.get("shared_context", {}),
            "modules": skeleton.get("modules", {}),
            "tasks": detailed_tasks,
        }
        self._apply_defaults(plan)
        return plan

    async def _detail_with_fallback(
        self,
        model: str,
        tasks: list[dict],
        spec_content: str,
        agents_md: str,
        project_name: str,
        label: str,
        is_canonical: bool = False,
    ) -> list[dict]:
        """Try primary model for Phase 2, fallback if it fails.
        If all detail calls fail, return skeleton tasks (never crash)."""
        try:
            result = await self._phase2_detail_batched(
                model, tasks, spec_content, agents_md, project_name, is_canonical,
            )
            logger.info(
                f"[GeminiPlanner] Phase 2 {label}: "
                f"{len(result)} tasks detailed with {model}"
            )
            return result
        except Exception as e:
            if model == self.model_fallback:
                logger.warning(
                    f"[GeminiPlanner] Phase 2 {label} failed with fallback model ({e}), "
                    f"using skeleton tasks"
                )
                return tasks  # Return skeleton instead of crashing
            logger.warning(
                f"[GeminiPlanner] Phase 2 {label} with {model} failed ({e}), "
                f"trying fallback {self.model_fallback}"
            )
            try:
                result = await self._phase2_detail_batched(
                    self.model_fallback, tasks, spec_content, agents_md, project_name,
                    is_canonical,
                )
                logger.info(
                    f"[GeminiPlanner] Phase 2 {label}: "
                    f"{len(result)} tasks detailed with {self.model_fallback} (fallback)"
                )
                return result
            except Exception as e2:
                logger.warning(
                    f"[GeminiPlanner] Phase 2 {label} fallback also failed ({e2}), "
                    f"using skeleton tasks"
                )
                return tasks  # Return skeleton instead of crashing

    async def _phase2_detail_batched(
        self,
        model: str,
        tasks: list[dict],
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> list[dict]:
        """Split large task lists into batches to avoid output truncation."""
        batch_size = self.max_tasks_per_detail
        if len(tasks) <= batch_size:
            return await self._phase2_detail(
                model, tasks, spec_content, agents_md, project_name, is_canonical,
            )

        # Split into batches
        batches = [tasks[i:i + batch_size] for i in range(0, len(tasks), batch_size)]
        logger.info(
            f"[GeminiPlanner] Splitting {len(tasks)} tasks into "
            f"{len(batches)} batches of ~{batch_size}"
        )

        all_detailed: list[dict] = []
        for i, batch in enumerate(batches):
            logger.info(
                f"[GeminiPlanner] Detail batch {i + 1}/{len(batches)}: "
                f"{len(batch)} tasks with {model}"
            )
            detailed = await self._phase2_detail(
                model, batch, spec_content, agents_md, project_name, is_canonical,
            )
            all_detailed.extend(detailed)

        return all_detailed

    async def _phase2_detail(
        self,
        model: str,
        task_skeletons: list[dict],
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> list[dict]:
        """Call model to produce detailed tasks from skeletons."""
        prompt = self._build_detail_prompt(
            task_skeletons, spec_content, agents_md, project_name, is_canonical,
        )
        raw = await self._call_model(model, prompt)
        parsed = self._parse_json(raw)

        # Handle {"tasks": [...]} or bare [...]
        if isinstance(parsed, dict):
            tasks = parsed.get("tasks", [])
        elif isinstance(parsed, list):
            tasks = parsed
        else:
            raise PlannerError(f"Phase 2 returned unexpected type: {type(parsed)}")

        if not tasks:
            raise PlannerError(f"Phase 2 ({model}) produced no tasks")

        return tasks

    # ══════════════════════════════════════════════════════════
    # Single-Phase Fallback (original v0.6 flow)
    # ══════════════════════════════════════════════════════════

    async def _plan_single_phase(
        self,
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> dict:
        """Original single-phase planning: spec → full plan in one shot."""
        prompt = self._build_planning_prompt(
            spec_content, agents_md, project_name, is_canonical,
        )

        if self.provider == "gemini":
            # Gemini CLI → API → Claude fallback
            try:
                logger.info("[GeminiPlanner] Single-phase: trying Gemini CLI...")
                result = await self._call_gemini_cli(prompt)
                plan = self._parse_plan(result)
                logger.info(
                    f"[GeminiPlanner] Plan via CLI: "
                    f"{len(plan.get('tasks', []))} tasks"
                )
                return plan
            except Exception as cli_err:
                logger.warning(f"[GeminiPlanner] Gemini CLI failed: {cli_err}")

            try:
                logger.info("[GeminiPlanner] Single-phase: trying Gemini API...")
                result = await self._call_gemini_api(prompt, self.model_classify)
                plan = self._parse_plan(result)
                logger.info(
                    f"[GeminiPlanner] Plan via API: "
                    f"{len(plan.get('tasks', []))} tasks"
                )
                return plan
            except Exception as api_err:
                logger.warning(f"[GeminiPlanner] Gemini API failed: {api_err}")

            logger.info(
                f"[GeminiPlanner] Single-phase: "
                f"Claude fallback ({self.model_fallback})..."
            )
            raw = await self._call_claude_cli(prompt, self.model_fallback)
            return self._parse_plan(raw)

        elif self.provider == "claude":
            logger.info(f"[GeminiPlanner] Single-phase with {self.model}")
            raw = await self._call_claude_cli(prompt, self.model)
            return self._parse_plan(raw)

        raise PlannerError(f"Unknown planner provider: {self.provider}")

    # ══════════════════════════════════════════════════════════
    # Model Dispatchers
    # ══════════════════════════════════════════════════════════

    async def _call_model(self, model: str, prompt: str) -> str:
        """Route to the right backend based on model prefix."""
        if model.startswith("gemini"):
            return await self._call_gemini(model, prompt)
        elif model.startswith("claude"):
            return await self._call_claude_cli(prompt, model)
        else:
            # deepseek, xai, openrouter, etc. → litellm
            return await self._call_litellm(model, prompt)

    async def _call_gemini(self, model: str, prompt: str) -> str:
        """Try Gemini CLI, then Gemini API."""
        try:
            return await self._call_gemini_cli(prompt)
        except Exception as e:
            logger.warning(f"[GeminiPlanner] Gemini CLI failed: {e}, trying API")
            return await self._call_gemini_api(prompt, model)

    async def _call_gemini_api(
        self, prompt: str, model: str | None = None,
    ) -> str:
        """Gemini API via google-generativeai SDK."""
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise PlannerError(
                "GOOGLE_API_KEY not set. "
                "Get one at https://aistudio.google.com/app/apikey"
            )

        try:
            import google.generativeai as genai
        except ImportError:
            raise PlannerError(
                "google-generativeai not installed. "
                "Run: pip install google-generativeai"
            )

        use_model = model or self.model_classify
        genai.configure(api_key=api_key)
        gm = genai.GenerativeModel(use_model)

        logger.info(f"[GeminiPlanner] Calling Gemini API: {use_model}")

        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: gm.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.2,
                        max_output_tokens=8192,
                    ),
                )
            ),
            timeout=self.timeout,
        )

        return response.text

    async def _call_gemini_cli(self, prompt: str) -> str:
        """Gemini CLI pipe mode."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            cmd = f"cat {prompt_file} | gemini"
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )

            if proc.returncode != 0:
                raise PlannerError(f"Gemini CLI failed: {stderr.decode()[:500]}")

            return stdout.decode()
        finally:
            os.unlink(prompt_file)

    # Machine-mode system prompt for planner Claude calls.
    # Overrides any CLAUDE.md conversational instructions.
    _PLANNER_SYSTEM_PROMPT = (
        "You are a machine JSON generator. "
        "Output ONLY the raw JSON object. "
        "No prose, no markdown fences, no Chinese text, no summaries, "
        "no follow-up questions, no greetings, no file writes. "
        "Your entire stdout must be parseable by json.loads()."
    )

    async def _call_claude_cli(
        self, prompt: str, model: str | None = None,
    ) -> str:
        """Claude CLI for text generation.

        Isolation strategy (no temp config dir needed):
        - --setting-sources ""  → disables all user/project CLAUDE.md + settings
        - --system-prompt       → explicit machine-mode instructions
        - cwd=/tmp              → extra guard against project CLAUDE.md
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            use_model = model or self.model_fallback
            cmd = (
                f"cat {prompt_file} | claude -p "
                f"--model {use_model} "
                f"--output-format text "
                f"--no-session-persistence "
                f'--setting-sources "" '
                f'--tools "" '
                f"--system-prompt {shlex.quote(self._PLANNER_SYSTEM_PROMPT)}"
            )
            # Multi-account: least-loaded account selection
            from src.auth.cli_runner import build_proc_env
            account_env = await get_pool().next_env(model=use_model)
            proc_env = build_proc_env(account_env)

            logger.info(
                f"[GeminiPlanner] Claude CLI running in isolated planner mode "
                f"(model={use_model}, settings=disabled)"
            )

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
                cwd="/tmp",  # avoid project CLAUDE.md
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )

            if proc.returncode != 0:
                logger.error(
                    f"[GeminiPlanner] Claude CLI exit={proc.returncode}, "
                    f"stdout={stdout.decode()[:200]!r}, "
                    f"stderr={stderr.decode()[:200]!r}"
                )
                raise PlannerError(
                    f"Claude CLI ({use_model}) exit={proc.returncode}: "
                    f"{stderr.decode()[:500] or stdout.decode()[:500]}"
                )

            return stdout.decode()
        finally:
            os.unlink(prompt_file)

    async def _call_litellm(self, model: str, prompt: str) -> str:
        """Call any model via litellm (deepseek, xai, openrouter, etc.)."""
        try:
            import litellm
        except ImportError:
            raise PlannerError(
                "litellm not installed. Run: pip install litellm"
            )

        logger.info(f"[GeminiPlanner] Calling litellm: {model}")

        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: litellm.completion(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=8192,
                )
            ),
            timeout=self.timeout,
        )

        return response.choices[0].message.content

    # ══════════════════════════════════════════════════════════
    # Output Constraints
    # ══════════════════════════════════════════════════════════

    # Shared strict output constraint appended to ALL planning prompts
    _OUTPUT_CONSTRAINT = """
## CRITICAL: Output Contract
You are a JSON generator, NOT a chat assistant.
- Output ONLY the raw JSON object required by the schema above.
- Do NOT include any explanation, summary, or commentary.
- Do NOT include markdown code fences (no ```json, no ```).
- Do NOT include any text before or after the JSON.
- Do NOT ask follow-up questions (e.g. "要開始執行嗎？", "ready to execute?").
- Do NOT announce completion (e.g. "plan generated", "產生完成", "here is the plan").
- Do NOT include any prose, greetings, or sign-offs.
- Your entire response must be parseable by json.loads() with zero preprocessing.
- If you cannot fully comply, still return best-effort valid JSON only.
"""

    # ══════════════════════════════════════════════════════════
    # Prompts
    # ══════════════════════════════════════════════════════════

    def _build_classify_prompt(
        self,
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> str:
        """Phase 1: classify tasks into backend/frontend/fullstack."""

        # SpecOS canonical spec preamble
        canonical_preamble = ""
        if is_canonical:
            canonical_preamble = """
## Spec Format Notice
This specification has been pre-processed by SpecOS into a **canonical planning spec**.
- All sections are normative and planning-relevant (gate policies, open questions, and UI-only sections have been removed).
- Section headers carry structured tags (e.g. `[api]`, `[state]`, `[data-model]`) — use these as complexity signals.
- Every requirement in this document is actionable. Do NOT skip or summarize sections.
- If the spec defines acceptance criteria or business scenarios, reference them in task acceptance_criteria.
"""

        # Enhanced complexity guidelines with domain signals
        complexity_section = """## Complexity Guidelines:
- **L** (Lite): Pure scaffolding, config files, boilerplate — does NOT import types/services/schemas produced by other tasks
- **S** (Simple): Simple logic but DOES import types/services/schemas from other tasks (e.g. CRUD using shared types, simple API route using a service)
- **M** (Medium): Requires reasoning — auth logic, business rules, middleware, validation, tests with edge cases
- **H** (Hard): Architecture decisions, security, payment, cross-module integration, complex state machines

### Domain-Specific Complexity Floors:
- Payment / billing / subscription / refund logic → **H minimum**
- Auth / RBAC / session / token management → **M minimum** (H if cross-module)
- State machines / workflow engines / order lifecycle → **H minimum**
- Schema / migration / data model with FK constraints → **M minimum**
- ERP / WMS / inventory / stock calculations → **M minimum**
- API contracts with >3 related endpoints → **M minimum**

Key distinction between L and S: if the task needs `import {{ SomeType }} from './other-task-output'`, it's at least S, not L."""

        return f"""You are a senior software architect. Read the project specification and break it into tasks with categories.

## Project: {project_name}
{canonical_preamble}
## Specification:
{spec_content}

{f"## Agent Rules:{chr(10)}{agents_md}" if agents_md else ""}

## Your Job:
1. Break the project into tasks (small, 5-10 minutes each)
2. Classify each task into ONE category:
   - "backend": API endpoints, database, service logic, auth, payment, middleware, data models, migrations, seed data
   - "frontend": UI components, pages, layouts, CSS/styling, animations, UX flows, client-side state
   - "fullstack": Cross-cutting tasks that span both frontend and backend (e.g. form + API + DB)
3. Assign complexity (L/S/M/H) and identify dependencies

{complexity_section}

## Output Format (JSON only, no markdown):

{{
  "project_name": "{project_name}",
  "shared_context": {{
    "tech_stack": "...",
    "conventions": "..."
  }},
  "modules": {{
    "module_name": {{
      "description": "...",
      "interface_files": [],
      "imports": [],
      "exports": []
    }}
  }},
  "tasks": [
    {{
      "id": "unique-uuid",
      "title": "Short descriptive title",
      "description": "Brief description (1-2 sentences) of what this task does",
      "category": "backend|frontend|fullstack",
      "complexity": "L|S|M|H",
      "module": "module_name",
      "dependencies": ["other-task-id"],
      "priority": 1
    }}
  ]
}}

## Rules:
1. Each task must be independently executable in an isolated git worktree
2. Tasks should be small enough to complete in 5-10 minutes
3. Wave 0 must include all shared types, interfaces, and DB schema
4. Dependencies must form a valid DAG (no cycles)
5. ALWAYS assign both category and complexity for every task
6. For projects with >15 tasks, split into modules with interface layers

{self._OUTPUT_CONSTRAINT}
"""

    def _build_detail_prompt(
        self,
        task_skeletons: list[dict],
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> str:
        """Phase 2: produce detailed implementation instructions for each task."""
        skeleton_json = json.dumps(task_skeletons, indent=2, ensure_ascii=False)

        # Canonical specs are already filtered — include full content
        if is_canonical:
            spec_section = spec_content
        else:
            # Truncate spec to save tokens (Phase 2 already has task context)
            spec_section = spec_content[:4000]
            if len(spec_content) > 4000:
                spec_section += "\n... (truncated)"

        canonical_note = ""
        if is_canonical:
            canonical_note = """
**Note**: This spec is a SpecOS canonical planning spec — all sections are normative and planning-relevant.
Use section tags and acceptance criteria from the spec to write precise implementation instructions.
If the spec defines state transitions, API contracts, or data models, reference them explicitly in the task description.
"""

        return f"""You are a senior software engineer. Refine the following task skeletons into detailed implementation plans.

## Project: {project_name}
{canonical_note}
## Specification (for context):
{spec_section}

{f"## Agent Rules:{chr(10)}{agents_md}" if agents_md else ""}

## Tasks to Detail:
{skeleton_json}

## Your Job:
For EACH task above, produce a detailed version with:
1. **description**: Full, specific instructions for an AI coding agent. Include:
   - What files to create/modify
   - What functions/classes to implement
   - What patterns to follow
   - Error handling requirements
2. **target_files**: Specific file paths that will be created or modified
3. **acceptance_criteria**: Testable conditions for success

IMPORTANT: Preserve the original id, title, category, complexity, module, dependencies, priority from the input. Do NOT add or remove tasks.

## Output Format (JSON only, no markdown):

{{
  "tasks": [
    {{
      "id": "same-as-input",
      "title": "same-as-input",
      "description": "DETAILED implementation instructions...",
      "category": "same-as-input",
      "agent_type": "",
      "complexity": "same-as-input",
      "module": "same-as-input",
      "target_files": ["src/path/to/file.ts"],
      "dependencies": ["same-as-input"],
      "acceptance_criteria": "Build passes, tests pass, ...",
      "priority": 1
    }}
  ]
}}

{self._OUTPUT_CONSTRAINT}
"""

    def _build_planning_prompt(
        self,
        spec_content: str,
        agents_md: str,
        project_name: str,
        is_canonical: bool = False,
    ) -> str:
        """Single-phase full planning prompt (fallback)."""

        canonical_preamble = ""
        if is_canonical:
            canonical_preamble = """
## Spec Format Notice
This specification has been pre-processed by SpecOS into a **canonical planning spec**.
- All sections are normative and planning-relevant (gate policies, open questions, and UI-only sections have been removed).
- Section headers carry structured tags (e.g. `[api]`, `[state]`, `[data-model]`) — use these as complexity signals.
- Every requirement in this document is actionable. Do NOT skip or summarize sections.
- If the spec defines acceptance criteria or business scenarios, reference them in task acceptance_criteria.
"""

        complexity_section = """## Complexity Guidelines:
- **L** (Lite): Pure scaffolding, config, boilerplate — does NOT import types/services from other tasks
- **S** (Simple): Simple logic but imports types/services/schemas from other tasks
- **M** (Medium): Auth logic, business rules, middleware, validation, tests with edge cases
- **H** (Hard): Architecture decisions, security/auth, payment, cross-module integration

### Domain-Specific Complexity Floors:
- Payment / billing / subscription / refund logic → **H minimum**
- Auth / RBAC / session / token management → **M minimum** (H if cross-module)
- State machines / workflow engines / order lifecycle → **H minimum**
- Schema / migration / data model with FK constraints → **M minimum**
- ERP / WMS / inventory / stock calculations → **M minimum**
- API contracts with >3 related endpoints → **M minimum**"""

        return f"""You are a senior software architect and project planner.

Read the following project specification and produce a detailed execution plan as JSON.

## Project: {project_name}
{canonical_preamble}
## Specification:
{spec_content}

{f"## Agent Rules:{chr(10)}{agents_md}" if agents_md else ""}

## Output Format (JSON only, no markdown):

{{
  "project_name": "{project_name}",
  "shared_context": {{
    "tech_stack": "...",
    "conventions": "..."
  }},
  "modules": {{
    "foundation": {{
      "description": "scaffold + DB + types + interfaces",
      "interface_files": ["src/interfaces/types.ts"],
      "imports": [],
      "exports": ["SharedTypes", "DBSchema"]
    }}
  }},
  "tasks": [
    {{
      "id": "unique-uuid",
      "title": "Short descriptive title",
      "description": "Full instructions for the agent. Be specific about what files to create/modify, what functions to implement, what patterns to follow.",
      "agent_type": "",
      "complexity": "L|S|M|H",
      "category": "backend|frontend|fullstack",
      "module": "foundation",
      "target_files": ["path/to/file.ts"],
      "dependencies": ["other-task-id"],
      "acceptance_criteria": "Testable conditions for success",
      "priority": 1
    }}
  ]
}}

{complexity_section}

## Category Guidelines:
- **backend**: API, database, service logic, auth, payment, middleware
- **frontend**: UI components, pages, layouts, CSS, animations, UX flows
- **fullstack**: Cross-cutting tasks spanning both frontend and backend

## Agent Type (leave empty for auto-routing, or specify):
- `claude_code`: For H complexity, architecture, security, auth, payment
- `""` (empty): Let the router decide automatically (recommended)

## Rules:
1. Each task must be independently executable in an isolated git worktree
2. Tasks should be small enough to complete in 5-10 minutes
3. Wave 0 must include all shared types, interfaces, and DB schema
4. Dependencies must form a valid DAG (no cycles)
5. Target files should be specific (not entire directories)
6. For projects with >15 tasks, split into modules with interface layers
7. Always assign complexity (L/S/M/H) and category (backend/frontend/fullstack)

{self._OUTPUT_CONSTRAINT}
"""

    # ══════════════════════════════════════════════════════════
    # Parsing
    # ══════════════════════════════════════════════════════════

    def _parse_plan(self, raw_output: str) -> dict:
        """
        Parse LLM output as plan JSON.
        Handles markdown code block wrapping.
        Validates 'tasks' field and sets defaults.
        """
        plan = self._parse_json(raw_output)
        if not isinstance(plan, dict):
            # Maybe it's a list of tasks directly
            if isinstance(plan, list) and plan and isinstance(plan[0], dict):
                logger.warning("[GeminiPlanner] Plan is a bare list, wrapping in dict")
                plan = {"tasks": plan}
            else:
                raise PlannerError(f"Plan is not a dict: {type(plan)}")

        if "tasks" not in plan:
            raise PlannerError("Plan missing 'tasks' field")

        self._apply_defaults(plan)
        return plan

    def _parse_json(self, raw_output: str):
        """Generic JSON parser — strips markdown code blocks and leading prose."""
        text = raw_output.strip()

        # Strip markdown code fences (```json ... ``` or ``` ... ```)
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # LLM sometimes prepends/appends prose around JSON.
        # Log a warning so we know the model misbehaved.
        first_brace = text.find("{")
        first_bracket = text.find("[")
        if first_brace == -1 and first_bracket == -1:
            # Log first 300 chars to help diagnose chat-style responses
            preview = text[:300].replace("\n", " ")
            logger.error(
                f"[GeminiPlanner] No JSON found in model output. "
                f"Model may have returned prose instead of JSON.\n"
                f"Preview: {preview}"
            )
            raise PlannerError(
                "No JSON found in LLM output — model returned prose instead of JSON"
            )

        # Warn: model didn't return clean JSON, extracting from prose
        prefix = text[:first_brace if first_brace != -1 else first_bracket].strip()
        if prefix:
            logger.warning(
                f"[GeminiPlanner] Model returned prose before JSON, "
                f"extracting embedded JSON. Prose prefix: {prefix[:100]}"
            )

        # Pick whichever comes first
        if first_bracket == -1 or (first_brace != -1 and first_brace < first_bracket):
            start_char, end_char = "{", "}"
            start_idx = first_brace
        else:
            start_char, end_char = "[", "]"
            start_idx = first_bracket

        # Find matching closing bracket by counting depth
        depth = 0
        end_idx = -1
        in_string = False
        escape_next = False
        for i in range(start_idx, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break

        if end_idx == -1:
            logger.error(
                f"[GeminiPlanner] Unbalanced JSON in output\n"
                f"Raw: {text[:500]}"
            )
            raise PlannerError("Unbalanced JSON in LLM output")

        extracted = text[start_idx:end_idx + 1]
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as e:
            logger.error(
                f"[GeminiPlanner] JSON parse failed after extraction: {e}\n"
                f"Raw: {extracted[:500]}"
            )
            raise PlannerError(f"Failed to parse JSON: {e}")

    @staticmethod
    def _apply_defaults(plan: dict):
        """Apply defaults to all tasks in plan."""
        for task in plan.get("tasks", []):
            task.setdefault("complexity", "M")
            task.setdefault("agent_type", "")
            task.setdefault("dependencies", [])
            task.setdefault("target_files", [])
            task.setdefault("module", "core")
            task.setdefault("category", "backend")
            # v2.0: gate fields (will be enriched later by Planner)
            task.setdefault("task_type", "")
            task.setdefault("gate_profile", {})
