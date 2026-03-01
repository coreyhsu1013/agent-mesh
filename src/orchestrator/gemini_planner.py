"""
Agent Mesh v0.6.0 — Gemini Planner
用 Gemini API 取代 Claude 做 planning，減少 Claude loading。

支援三種模式：
1. Gemini API（推薦，最穩定）
2. Gemini CLI（如果支援 pipe mode）
3. Claude CLI（fallback）
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)


class PlannerError(Exception):
    pass


class GeminiPlanner:
    """
    用 Gemini 產生 plan.json。

    Gemini 2.0 Flash 特點：
    - 便宜（免費額度大）
    - 速度快
    - 長 context（100萬+ tokens）
    - planning 品質對標 GPT-4o
    """

    def __init__(self, config: dict):
        planner_cfg = config.get("planner", {})
        self.provider = planner_cfg.get("provider", "gemini")
        self.model = planner_cfg.get("model", "gemini-2.0-flash")
        self.fallback = planner_cfg.get("fallback", "claude")
        self.timeout = planner_cfg.get("timeout", 300)

    async def plan(
        self,
        spec_content: str,
        agents_md: str = "",
        project_name: str = "project",
    ) -> dict:
        """
        讀 spec → 產生 plan.json。

        Args:
            spec_content: 專案規格文件內容
            agents_md: AGENTS.md 內容（任務分配規則）
            project_name: 專案名稱
        Returns:
            plan.json dict
        """
        prompt = self._build_planning_prompt(spec_content, agents_md, project_name)

        if self.provider == "gemini":
            # 優先嘗試 Gemini CLI（已驗證支援 pipe mode）
            try:
                logger.info("[GeminiPlanner] Trying Gemini CLI (pipe mode)...")
                result = await self._call_gemini_cli(prompt)
                plan = self._parse_plan(result)
                logger.info(f"[GeminiPlanner] Plan generated via CLI: {len(plan.get('tasks', []))} tasks")
                return plan
            except Exception as cli_err:
                logger.warning(f"[GeminiPlanner] Gemini CLI failed: {cli_err}")

            # Fallback: Gemini API（需要 GOOGLE_API_KEY）
            try:
                logger.info("[GeminiPlanner] Trying Gemini API fallback...")
                result = await self._call_gemini_api(prompt)
                plan = self._parse_plan(result)
                logger.info(f"[GeminiPlanner] Plan generated via API: {len(plan.get('tasks', []))} tasks")
                return plan
            except Exception as api_err:
                logger.warning(f"[GeminiPlanner] Gemini API also failed: {api_err}")

            # Final fallback: Claude
            if self.fallback == "claude":
                logger.info("[GeminiPlanner] Falling back to Claude CLI...")
                return await self._call_claude_fallback(prompt)

            raise PlannerError(f"All Gemini methods failed. CLI: {cli_err}, API: {api_err}")

        # Claude as primary
        if self.provider == "claude":
            return await self._call_claude_fallback(prompt)

        raise PlannerError(f"Unknown planner provider: {self.provider}")

    async def _call_gemini_api(self, prompt: str) -> str:
        """
        呼叫 Gemini API（google-generativeai SDK）。
        需要 GOOGLE_API_KEY 環境變數。
        """
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

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(self.model)

        logger.info(f"[GeminiPlanner] Calling {self.model}...")

        # Gemini API 是同步的，用 run_in_executor 包裝成 async
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: model.generate_content(
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
        """
        嘗試用 Gemini CLI（如果支援非互動模式）。
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            # 嘗試 pipe mode
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

    async def _call_claude_fallback(self, prompt: str) -> dict:
        """
        Claude CLI fallback（跟 v0.5.0 一樣）。
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            cmd = f"cat {prompt_file} | claude --output-format text"
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )

            if proc.returncode != 0:
                raise PlannerError(f"Claude fallback failed: {stderr.decode()[:500]}")

            result = stdout.decode()
            return self._parse_plan(result)
        finally:
            os.unlink(prompt_file)

    def _parse_plan(self, raw_output: str) -> dict:
        """
        從 LLM 輸出中提取 JSON plan。
        處理 markdown code block 包裝。
        """
        text = raw_output.strip()

        # 移除 markdown code block
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        try:
            plan = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"[GeminiPlanner] JSON parse failed: {e}\nRaw: {text[:500]}")
            raise PlannerError(f"Failed to parse plan JSON: {e}")

        # 驗證必要欄位
        if "tasks" not in plan:
            raise PlannerError("Plan missing 'tasks' field")

        # 設定 defaults
        for task in plan["tasks"]:
            task.setdefault("complexity", "M")
            task.setdefault("agent_type", "")  # 空 = 自動路由
            task.setdefault("dependencies", [])
            task.setdefault("target_files", [])
            task.setdefault("module", "core")

        return plan

    def _build_planning_prompt(
        self,
        spec_content: str,
        agents_md: str,
        project_name: str,
    ) -> str:
        """
        建構 planning prompt。
        v0.6.0 改進：加入 complexity 和 agent_type 建議。
        """
        return f"""You are a senior software architect and project planner.

Read the following project specification and produce a detailed execution plan as JSON.

## Project: {project_name}

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
      "complexity": "L|M|H",
      "module": "foundation",
      "target_files": ["path/to/file.ts"],
      "dependencies": ["other-task-id"],
      "acceptance_criteria": "Testable conditions for success",
      "priority": 1
    }}
  ]
}}

## Complexity Guidelines:
- **L** (Low): CRUD, boilerplate, documentation, CSS, i18n, seed data, simple tests
- **M** (Medium): API endpoints, service logic, components with state, database queries
- **H** (High): Architecture decisions, security/auth, payment, complex integrations, DB migrations, abstract patterns

## Agent Type (leave empty for auto-routing, or specify):
- `claude_code`: For H complexity, architecture, security, auth, payment
- `deepseek_aider`: For L/M complexity, CRUD, boilerplate, refactoring, tests
- `""` (empty): Let the router decide automatically (recommended)

## Rules:
1. Each task must be independently executable in an isolated git worktree
2. Tasks should be small enough to complete in 5-10 minutes
3. Wave 0 must include all shared types, interfaces, and DB schema
4. Dependencies must form a valid DAG (no cycles)
5. Target files should be specific (not entire directories)
6. For projects with >15 tasks, split into modules with interface layers
7. Always assign complexity (L/M/H) to help the router

Output ONLY valid JSON. No explanation, no markdown code blocks.
"""
