---
description: Python coding conventions for agent-mesh
globs: "**/*.py"
---

# Python Style Rules

## Async
- All orchestrator code is async. Use `async def` + `await`, never blocking I/O.
- Entry point uses `asyncio.run()`.
- Use `asyncio.gather()` for parallel work, `asyncio.Semaphore` for concurrency limits.

## Type Hints
- Full type hints on all function signatures (Python 3.11+ syntax).
- Use `X | None` not `Optional[X]`. Use `list[X]` not `List[X]`.
- Dataclasses with `@dataclass` for all data models.

## Logging
- Use `logging.getLogger(__name__)` per module.
- Log levels: `info` for milestones, `debug` for details, `warning` for recoverable issues, `error` for failures.
- Include task_id/module context in log messages.
- Use emoji prefixes in user-facing output: ✅ success, ❌ failure, ⏳ in-progress, 🔄 retry.

## Error Handling
- Catch specific exceptions, never bare `except:`.
- Agent runner failures should return `RunResult(success=False)`, not raise.
- Let `asyncio.CancelledError` propagate.

## Naming
- snake_case for functions/variables, PascalCase for classes.
- Private methods prefixed with `_`.
- Constants in UPPER_SNAKE_CASE.

## Comments
- Chinese/English mix is intentional and accepted.
- Inline comments for non-obvious logic only.
