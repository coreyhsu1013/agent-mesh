# src/auth — CLI Agent Runners

Wraps external CLI tools (claude, aider, gemini) for task execution.

## Modules

| Module | Purpose |
|--------|---------|
| `aider_runner.py` | `AiderRunner` for Grok/DeepSeek, `ClaudeRunner` for Claude tasks |
| `cli_runner.py` | `run_claude_prompt()` and `run_gemini_prompt()` for planning/review |
| `check.py` | Pre-execution auth validation: `check_cli()`, `check_all_required()` |

## Key Patterns

### Heartbeat-based timeout (AiderRunner)
- NOT a fixed duration timeout — monitors stdout for idle time
- If no new output for `heartbeat_timeout` seconds → kill process
- Prevents hanging on stuck API calls while allowing long-running legitimate work
- `_monitor_heartbeat()` runs as async task alongside the process

### ClaudeRunner
- Parses `claude --output-format stream-json` for structured results
- Extracts cost info from stream events
- Returns `RunResult(success, stdout, stderr, error, cost_result)`

### CLI prompt piping (cli_runner.py)
- Writes prompt to temp file, passes via stdin to avoid arg length limits
- `run_claude_prompt()`: `claude -p --output-format stream-json < tmpfile`
- `run_gemini_prompt()`: `gemini < tmpfile`

### RunResult
```python
@dataclass
class RunResult:
    success: bool
    stdout: str
    stderr: str
    error: str | None
    cost_result: CostResult | None
```
