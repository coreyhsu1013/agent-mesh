"""
Agent Mesh v0.7 — CLI Entry Point

v0.7:
- Project-level ReAct loop (verify → fix-plan → execute → repeat)
- Dual-model verification (Gemini + Opus spec diff)
- Conflict marker scan + auto-fix
- --verify / --cycles / --fix-plan flags

v0.6.5:
- Claude Opus/Sonnet 分流
- DeepSeek reasoner/chat 分流
- WorkspacePool per-task isolation
- --resume 從 DB 讀 completed 狀態
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import yaml

logger = logging.getLogger("agent-mesh")

DEFAULT_CONFIG = {
    "version": "0.7.0",
    "planner": {
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "fallback": "claude",
        "timeout": 300,
    },
    "agents": {
        "claude_code": {
            "enabled": True,
            "model_opus": "claude-opus-4-6",
            "timeout_opus": 1200,
            "model_sonnet": "claude-sonnet-4-6",
            "timeout_sonnet": 600,
            "max_parallel": 2,
        },
        "deepseek_aider": {
            "enabled": True,
            "model_reasoner": "deepseek/deepseek-reasoner",
            "timeout_reasoner": 600,
            "model_chat": "deepseek/deepseek-chat",
            "timeout_chat": 300,
            "api_key_env": "DEEPSEEK_API_KEY",
            "max_parallel": 3,
        },
    },
    "react": {
        "max_attempts": 3,
        "run_tests": True,
        "run_build": True,
        "run_lint": False,
        "retry_delay_base": 5,
    },
    "reviewer": {
        "provider": "claude",
        "model": "claude-opus-4-6",
        "auto_approve_on_attempt": 3,
        "diff_max_chars": 10000,
        "timeout": 120,
    },
    "dispatcher": {
        "max_parallel": 4,
        "semaphore_claude": 2,
        "semaphore_deepseek": 3,
        "retry_delay": 30,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_path: str | None = None) -> dict:
    """Load config from file, merged with defaults."""
    config = DEFAULT_CONFIG.copy()

    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
        config = deep_merge(config, user_config)
        logger.info(f"[Config] Loaded: {config_path}")
    else:
        # Try default locations
        for path in ["config.yaml", "agent-mesh.yaml"]:
            if os.path.exists(path):
                with open(path) as f:
                    user_config = yaml.safe_load(f) or {}
                config = deep_merge(config, user_config)
                logger.info(f"[Config] Loaded: {path}")
                break

    return config


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)
    # Quiet noisy loggers
    for name in ["httpx", "httpcore", "urllib3", "google"]:
        logging.getLogger(name).setLevel(logging.WARNING)


async def run_plan_only(config: dict, spec_path: str, repo_dir: str):
    """Generate plan from spec without executing."""
    from .planner import Planner

    planner = Planner(config, repo_dir)

    logger.info(f"\n🚀 Agent Mesh v{config.get('version', '0.6.5')}")
    logger.info(f"📋 Planning from: {spec_path}")

    plan = await planner.plan(spec_path)

    if plan is None:
        logger.error("❌ Planning failed")
        sys.exit(1)

    # Save plan
    plan_path = os.path.join(repo_dir, "plan.json")
    plan_data = plan.to_dict()
    with open(plan_path, "w") as f:
        json.dump(plan_data, f, indent=2, ensure_ascii=False)

    logger.info(f"\n✅ Plan saved to: {plan_path}")
    logger.info(f"   {len(plan.tasks)} tasks across {len(set(t.module for t in plan.tasks))} modules")

    # Show routing preview
    from .router import ModelRouter
    router = ModelRouter(config)
    summary = router.get_routing_summary(plan.tasks)
    total = len(plan.tasks)

    logger.info("\n🤖 Routing Preview:")
    for agent_model, titles in summary.items():
        pct = len(titles) / total * 100
        logger.info(f"  {agent_model}: {len(titles)} ({pct:.0f}%)")
        for t in titles:
            logger.info(f"    • {t}")


async def run_execute(config: dict, plan_path: str, repo_dir: str,
                      modules: list[str] | None = None,
                      waves: list[int] | None = None,
                      resume: bool = False):
    """Execute plan."""
    from ..context.store import ContextStore
    from ..models.task import TaskPlan
    from .dispatcher import Dispatcher

    # Load plan
    with open(plan_path) as f:
        plan_data = json.load(f)
    plan = TaskPlan.from_dict(plan_data)

    logger.info(f"\n🚀 Agent Mesh v{config.get('version', '0.7.0')}")
    logger.info(f"📋 Executing: {plan_path} ({len(plan.tasks)} tasks)")
    logger.info(f"📁 Repo: {repo_dir}")
    if resume:
        logger.info("🔄 Resume mode: skipping completed tasks from DB")

    # Init store
    store = ContextStore(repo_dir)
    run_id = store.save_plan(plan)

    # Execute
    dispatcher = Dispatcher(config, repo_dir, store)
    await dispatcher.execute_plan(
        plan=plan,
        run_id=run_id,
        modules=modules,
        waves=waves,
        resume=resume,
    )

    store.close()


async def run_verify(config: dict, repo_dir: str, spec_path: str | None = None,
                     fix_plan: bool = False):
    """Verify repo and optionally generate fix-plan."""
    from .project_loop import ProjectLoop

    logger.info(f"\n🔍 Agent Mesh v{config.get('version', '0.7.0')} — Verify Mode")
    logger.info(f"📁 Repo: {repo_dir}")
    if spec_path:
        logger.info(f"📋 Spec: {spec_path}")

    loop = ProjectLoop(config, repo_dir, spec_path)

    if fix_plan:
        report, plan = await loop.verify_and_plan(cycle=1)
        if plan:
            plan_path = os.path.join(repo_dir, ".agent-mesh/fix-plan-1.json")
            logger.info(f"\n📋 Fix-plan: {plan_path}")
            logger.info(f"   {len(plan['tasks'])} fix tasks from {plan['shared_context'].get('original_issue_count', '?')} issues")
            logger.info(f"\n   Run with: python3 -m src.orchestrator.main --plan {plan_path} --repo {repo_dir} --no-review -v")
    else:
        report = await loop.verify(cycle=1)

    return report


async def run_cycles(config: dict, repo_dir: str, spec_path: str,
                     initial_plan: str, max_cycles: int = 5,
                     max_parallel: int = 3, no_review: bool = True):
    """Run project-level ReAct loop with auto cycles."""
    from .project_loop import ProjectLoop

    logger.info(f"\n🔄 Agent Mesh v{config.get('version', '0.7.0')} — Auto Cycle Mode")
    logger.info(f"📁 Repo: {repo_dir}")
    logger.info(f"📋 Spec: {spec_path}")
    logger.info(f"🔄 Max cycles: {max_cycles}")

    loop = ProjectLoop(config, repo_dir, spec_path)

    # For auto mode, we need a dispatcher factory
    # (will be implemented when auto-cycle execution is ready)
    success = await loop.run_auto(
        max_cycles=max_cycles,
        initial_plan_path=initial_plan,
        max_parallel=max_parallel,
        no_review=no_review,
    )

    return success


def main():
    parser = argparse.ArgumentParser(
        description="Agent Mesh v0.7 — Multi-Agent CLI Orchestration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Plan from spec (Gemini)
  python3 -m src.orchestrator.main --spec spec.md --repo ~/project --plan-only

  # Execute plan
  python3 -m src.orchestrator.main --plan plan.json --repo ~/project --no-review

  # Resume failed tasks
  python3 -m src.orchestrator.main --plan plan.json --repo ~/project --resume

  # Verify only (mechanical checks)
  python3 -m src.orchestrator.main --repo ~/project --verify

  # Verify with spec diff + generate fix-plan
  python3 -m src.orchestrator.main --repo ~/project --verify --spec spec.md --fix-plan

  # Auto-cycle to convergence (max 5 rounds)
  python3 -m src.orchestrator.main --spec spec.md --plan plan.json --repo ~/project --cycles 5 --no-review -v
        """,
    )

    parser.add_argument("--spec", help="Path to spec.md for planning or verification")
    parser.add_argument("--plan", help="Path to plan.json for execution")
    parser.add_argument("--repo", required=True, help="Target repository path")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--plan-only", action="store_true", help="Generate plan only")
    parser.add_argument("--resume", action="store_true", help="Resume: skip completed tasks")
    parser.add_argument("--module", nargs="+", help="Filter by module names")
    parser.add_argument("--waves", nargs="+", type=int, help="Execute specific wave numbers")
    parser.add_argument("--max-parallel", type=int, help="Override max parallel tasks")
    parser.add_argument("--no-review", action="store_true", help="Skip code review")
    # v0.7 flags
    parser.add_argument("--verify", action="store_true", help="Run verification checks")
    parser.add_argument("--fix-plan", action="store_true", help="Generate fix-plan from verify")
    parser.add_argument("--cycles", type=int, help="Auto-cycle mode: max number of cycles")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Resolve repo path
    repo_dir = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo_dir):
        logger.error(f"Repo directory not found: {repo_dir}")
        sys.exit(1)

    # Load config
    config = load_config(args.config)

    # Apply CLI overrides
    if args.max_parallel:
        config["dispatcher"]["max_parallel"] = args.max_parallel
    if args.no_review:
        config["no_review"] = True

    # Route to action
    if args.verify:
        # v0.7: Verify mode
        spec_path = os.path.abspath(args.spec) if args.spec else None
        asyncio.run(run_verify(config, repo_dir, spec_path, fix_plan=args.fix_plan))

    elif args.cycles:
        # v0.7: Auto-cycle mode
        if not args.spec:
            logger.error("--spec is required for --cycles mode")
            sys.exit(1)
        if not args.plan:
            logger.error("--plan is required for --cycles mode (initial plan)")
            sys.exit(1)
        asyncio.run(run_cycles(
            config, repo_dir,
            spec_path=os.path.abspath(args.spec),
            initial_plan=os.path.abspath(args.plan),
            max_cycles=args.cycles,
            max_parallel=args.max_parallel or 3,
            no_review=args.no_review,
        ))

    elif args.plan_only or (args.spec and not args.plan):
        if not args.spec:
            logger.error("--spec is required for planning")
            sys.exit(1)
        asyncio.run(run_plan_only(config, args.spec, repo_dir))

    elif args.plan:
        plan_path = os.path.abspath(args.plan)
        if not os.path.exists(plan_path):
            logger.error(f"Plan not found: {plan_path}")
            sys.exit(1)
        asyncio.run(run_execute(
            config, plan_path, repo_dir,
            modules=args.module,
            waves=args.waves,
            resume=args.resume,
        ))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
