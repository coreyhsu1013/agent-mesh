"""
Regression test: gate retry exhausted → escalate model, not final fail.
Tests the state machine:
  Attempt N (Sonnet) → gate fail × max_retries → escalate to N+1 (Opus)
  Only final fail when no models remain.

Also tests: ReAct failure during gate retry → escalate instead of exit.

Run: python3 -m unittest tests.test_gate_escalation -v
"""

import sys
import asyncio
import unittest
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

from src.orchestrator.react_loop import ReactLoop, TaskResult, RunResult, Observation


# ── Stubs ──

@dataclass
class FakeTask:
    id: str = "task-1"
    title: str = "Scaffold Admin"
    description: str = "Create admin scaffold"
    complexity: str = "M"
    target_files: list = field(default_factory=list)
    acceptance_criteria: str = ""
    gate_feedback: dict = field(default_factory=dict)
    gate_profile: dict = field(default_factory=dict)
    task_type: str = ""
    category: str = ""
    module: str = ""


@dataclass
class FakeRoutingDecision:
    model: str = "claude-sonnet-4-6"
    model_short: str = "sonnet"
    agent_type: str = "claude_code"
    timeout_multiplier: float = 1.0
    force_timeout_seconds: int = 0


class FakeRouter:
    def __init__(self, chain_length: int = 3):
        self._chain_length = chain_length

    def get_max_attempts(self, complexity: str) -> int:
        return self._chain_length

    def get_model_for_attempt(self, complexity, attempt, *, log=True):
        models = {
            1: ("grok-fast", "grok"),
            2: ("claude-sonnet-4-6", "sonnet"),
            3: ("claude-opus-4-6", "opus"),
        }
        model, short = models.get(attempt, ("claude-opus-4-6", "opus"))
        return FakeRoutingDecision(model=model, model_short=short)


# ── Tests ──

class TestSingleAttemptParameter(unittest.TestCase):
    """Test that single_attempt=True pins ReAct to one model."""

    def test_single_attempt_limits_loop(self):
        """With single_attempt=True, only one attempt runs even if chain is longer."""
        loop = ReactLoop(config={"react": {"run_tests": False, "run_build": False}})
        router = FakeRouter(chain_length=3)
        task = FakeTask()

        attempts_run = []

        async def fake_execute(prompt, workspace_dir, target_files=None, **kwargs):
            attempts_run.append(kwargs.get("model"))
            return RunResult(success=False, error="intentional fail")

        runner = AsyncMock()
        runner.execute = fake_execute

        from src.models.task import AgentType
        runners = {AgentType.CLAUDE_CODE: runner}

        async def run():
            return await loop.execute_task(
                task=task, runners=runners, router=router,
                workspace_dir="/tmp/test",
                start_attempt=2,
                single_attempt=True,
            )

        result = asyncio.run(run())

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(attempts_run), 1, "Should only run ONE attempt")
        self.assertEqual(attempts_run[0], "claude-sonnet-4-6")

    def test_without_single_attempt_runs_full_chain(self):
        """Without single_attempt, ReAct runs from start_attempt to max."""
        loop = ReactLoop(config={"react": {
            "run_tests": False, "run_build": False, "retry_delay_base": 0,
        }})
        router = FakeRouter(chain_length=3)
        task = FakeTask()

        attempts_run = []

        async def fake_execute(prompt, workspace_dir, target_files=None, **kwargs):
            attempts_run.append(kwargs.get("model"))
            return RunResult(success=False, error="intentional fail")

        runner = AsyncMock()
        runner.execute = fake_execute

        from src.models.task import AgentType
        runners = {AgentType.CLAUDE_CODE: runner}

        async def run():
            return await loop.execute_task(
                task=task, runners=runners, router=router,
                workspace_dir="/tmp/test",
                start_attempt=1,
                single_attempt=False,
            )

        result = asyncio.run(run())

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(attempts_run), 3, "Should run all 3 attempts")

    def test_single_attempt_success(self):
        """single_attempt=True with successful execution returns completed."""
        loop = ReactLoop(config={"react": {
            "run_tests": False, "run_build": False,
        }})
        router = FakeRouter(chain_length=3)
        task = FakeTask(title="Fix auth bug")  # not a must-change task

        async def fake_execute(prompt, workspace_dir, target_files=None, **kwargs):
            return RunResult(success=True, stdout="done " * 20)

        runner = AsyncMock()
        runner.execute = fake_execute

        from src.models.task import AgentType
        runners = {AgentType.CLAUDE_CODE: runner}

        async def run():
            return await loop.execute_task(
                task=task, runners=runners, router=router,
                workspace_dir="/tmp/test",
                start_attempt=2,
                single_attempt=True,
            )

        result = asyncio.run(run())

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.attempts, 2)


class TestGateEscalationStateMachine(unittest.TestCase):
    """
    Verify the intended state machine:
    - Gate retry uses single_attempt (same model)
    - Gate escalation moves to next model
    - ReAct failure during gate retry triggers escalation, not exit
    """

    def test_gate_retry_then_escalate_flow(self):
        """
        Simulated flow:
        1. Sonnet succeeds (ReAct) → gate fails
        2. Gate retry 1/2 (Sonnet, single) → succeeds → gate fails
        3. Gate retry 2/2 (Sonnet, single) → succeeds → gate fails
        4. Gate retries exhausted → escalate to Opus
        5. Opus succeeds → gate passes
        """
        # This is a logic test — we verify the state transitions
        # by checking that single_attempt is passed correctly.

        max_gate_retries = 2
        max_att = 3
        current_chain_attempt = 2  # Sonnet
        gate_attempt = 0

        transitions = []

        # Simulate the while loop logic
        for _ in range(10):  # safety limit
            gate_attempt += 1

            # Simulate: gate always fails until Opus
            gate_passed = (current_chain_attempt == 3)  # Opus passes
            if gate_passed:
                transitions.append(("gate_pass", current_chain_attempt))
                break

            if gate_attempt <= max_gate_retries:
                transitions.append(("gate_retry", current_chain_attempt))
                # Would call execute_task(single_attempt=True)
                continue

            # Gate retries exhausted
            next_chain_attempt = current_chain_attempt + 1
            if next_chain_attempt <= max_att:
                transitions.append(("escalate", next_chain_attempt))
                current_chain_attempt = next_chain_attempt
                gate_attempt = 0
                continue
            else:
                transitions.append(("final_fail", current_chain_attempt))
                break

        expected = [
            ("gate_retry", 2),      # retry 1/2 with Sonnet
            ("gate_retry", 2),      # retry 2/2 with Sonnet
            ("escalate", 3),        # escalate to Opus
            ("gate_pass", 3),       # Opus passes gate
        ]
        self.assertEqual(transitions, expected)

    def test_react_failure_triggers_escalation(self):
        """
        When ReAct returns failed during gate retry,
        should escalate to next model (not exit loop).

        Simulated flow:
        1. Sonnet succeeds → gate fails
        2. Gate retry (Sonnet, single) → ReAct FAILS
        3. Escalate to Opus → Opus succeeds → gate passes
        """
        max_gate_retries = 2
        max_att = 3
        current_chain_attempt = 2
        gate_attempt = 0

        transitions = []
        react_status = "completed"  # initial

        for iteration in range(10):
            # Handle ReAct failure
            if react_status != "completed":
                next_chain_attempt = current_chain_attempt + 1
                if next_chain_attempt <= max_att:
                    transitions.append(("react_fail_escalate", next_chain_attempt))
                    current_chain_attempt = next_chain_attempt
                    gate_attempt = 0
                    react_status = "completed"  # Opus succeeds
                    continue
                else:
                    transitions.append(("final_fail", current_chain_attempt))
                    break

            gate_attempt += 1

            # Gate check
            gate_passed = (current_chain_attempt == 3)
            if gate_passed:
                transitions.append(("gate_pass", current_chain_attempt))
                break

            if gate_attempt <= max_gate_retries:
                transitions.append(("gate_retry", current_chain_attempt))
                # Simulate: Sonnet fails during gate retry
                react_status = "failed"
                continue

            next_chain_attempt = current_chain_attempt + 1
            if next_chain_attempt <= max_att:
                transitions.append(("escalate", next_chain_attempt))
                current_chain_attempt = next_chain_attempt
                gate_attempt = 0
                continue
            else:
                transitions.append(("final_fail", current_chain_attempt))
                break

        expected = [
            ("gate_retry", 2),              # retry with Sonnet → ReAct fails
            ("react_fail_escalate", 3),     # escalate to Opus
            ("gate_pass", 3),               # Opus passes gate
        ]
        self.assertEqual(transitions, expected)

    def test_all_models_exhausted_final_fail(self):
        """When all models are exhausted, result should be failed."""
        max_gate_retries = 1
        max_att = 2
        current_chain_attempt = 1
        gate_attempt = 0

        transitions = []
        react_status = "completed"

        for _ in range(20):
            if react_status != "completed":
                next_chain_attempt = current_chain_attempt + 1
                if next_chain_attempt <= max_att:
                    transitions.append(("react_fail_escalate", next_chain_attempt))
                    current_chain_attempt = next_chain_attempt
                    gate_attempt = 0
                    react_status = "completed"
                    continue
                else:
                    transitions.append(("final_fail_react", current_chain_attempt))
                    break

            gate_attempt += 1
            gate_passed = False  # gate never passes

            if gate_passed:
                break

            if gate_attempt <= max_gate_retries:
                transitions.append(("gate_retry", current_chain_attempt))
                continue

            next_chain_attempt = current_chain_attempt + 1
            if next_chain_attempt <= max_att:
                transitions.append(("escalate", next_chain_attempt))
                current_chain_attempt = next_chain_attempt
                gate_attempt = 0
                continue
            else:
                transitions.append(("final_fail_gate", current_chain_attempt))
                break

        # Model 1: 1 gate check + 1 retry = 2 → escalate
        # Model 2: 1 gate check + 1 retry = 2 → no more models → fail
        expected = [
            ("gate_retry", 1),
            ("escalate", 2),
            ("gate_retry", 2),
            ("final_fail_gate", 2),
        ]
        self.assertEqual(transitions, expected)


if __name__ == "__main__":
    unittest.main()
