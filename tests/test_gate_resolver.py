"""Tests for gate registry task_type routing."""
import pytest
from src.models.task import Task, GateProfile
from src.gates.registry import GateRegistry


@pytest.fixture
def registry():
    return GateRegistry()


class TestTaskTypeRouting:
    """v2.1: task_type → profile mapping."""

    def test_analysis_gate(self, registry):
        task = Task(title="Research auth", task_type="analysis")
        profile = registry.resolve_profile(task)
        assert profile.name == "analysis_gate"

    def test_schema_gate(self, registry):
        task = Task(title="Update schema", task_type="schema")
        profile = registry.resolve_profile(task)
        assert profile.name == "schema_critical"

    def test_migration_gate(self, registry):
        task = Task(title="Run migration", task_type="migration")
        profile = registry.resolve_profile(task)
        assert profile.name == "schema_critical"

    def test_service_gate(self, registry):
        task = Task(title="Add user service", task_type="service")
        profile = registry.resolve_profile(task)
        assert profile.name == "critical_backend"

    def test_router_gate(self, registry):
        task = Task(title="Add route", task_type="router")
        profile = registry.resolve_profile(task)
        assert profile.name == "api_basic"

    def test_frontend_gate(self, registry):
        task = Task(title="Add page", task_type="frontend")
        profile = registry.resolve_profile(task)
        assert profile.name == "ui_operability_basic"

    def test_test_only_gate(self, registry):
        task = Task(title="Add tests", task_type="test_only")
        profile = registry.resolve_profile(task)
        assert profile.name == "test_gate"

    def test_config_gate(self, registry):
        task = Task(title="Update config", task_type="config")
        profile = registry.resolve_profile(task)
        assert profile.name == "coding_basic"

    def test_listener_gate(self, registry):
        task = Task(title="Add listener", task_type="listener")
        profile = registry.resolve_profile(task)
        assert profile.name == "integration_basic"

    def test_scheduler_gate(self, registry):
        task = Task(title="Add scheduler", task_type="scheduler")
        profile = registry.resolve_profile(task)
        assert profile.name == "integration_basic"


class TestPrecedence:
    """Verify resolution order: explicit > task_type > heuristic."""

    def test_explicit_profile_overrides_task_type(self, registry):
        """Explicit gate_profile dict takes precedence over task_type."""
        task = Task(
            title="Critical auth",
            task_type="analysis",  # would normally → analysis_gate
            gate_profile={"name": "critical_backend"},
        )
        profile = registry.resolve_profile(task)
        assert profile.name == "critical_backend"

    def test_task_type_overrides_heuristic(self, registry):
        """task_type takes precedence over keyword heuristic."""
        task = Task(
            title="Create API endpoint for products",  # heuristic → api_basic
            task_type="service",  # explicit → critical_backend
        )
        profile = registry.resolve_profile(task)
        assert profile.name == "critical_backend"

    def test_empty_task_type_falls_through(self, registry):
        """Empty task_type falls through to heuristic resolution."""
        task = Task(
            title="Add API endpoint",
            task_type="",  # empty → fall through
        )
        profile = registry.resolve_profile(task)
        assert profile.name == "api_basic"  # matched by heuristic

    def test_unknown_task_type_falls_through(self, registry):
        """Unknown task_type falls through to heuristic."""
        task = Task(
            title="Add API endpoint",
            task_type="custom_unknown",  # not in mapping
        )
        profile = registry.resolve_profile(task)
        assert profile.name == "api_basic"  # matched by heuristic


class TestAnalysisGateProfile:
    """Verify analysis_gate profile properties."""

    def test_no_input_checks(self, registry):
        profile = registry.get_profile("analysis_gate")
        assert profile.input_checks == []

    def test_no_verification_checks(self, registry):
        profile = registry.get_profile("analysis_gate")
        assert profile.verification_checks == []

    def test_minimal_rule_checks(self, registry):
        profile = registry.get_profile("analysis_gate")
        assert "no_runtime_modification" in profile.rule_checks
        assert "no_build_artifacts" in profile.rule_checks


class TestTestGateProfile:
    """Verify test_gate profile properties."""

    def test_has_target_files_check(self, registry):
        profile = registry.get_profile("test_gate")
        assert "target_files_defined" in profile.input_checks

    def test_has_diff_and_tests(self, registry):
        profile = registry.get_profile("test_gate")
        assert "diff_not_empty" in profile.verification_checks
        assert "tests_pass" in profile.verification_checks


class TestDodChecks:
    """Verify DoD checks are in the right profiles."""

    def test_coding_basic_has_dod(self, registry):
        profile = registry.get_profile("coding_basic")
        assert "dod_diff_required" in profile.verification_checks

    def test_critical_backend_has_must_change(self, registry):
        profile = registry.get_profile("critical_backend")
        assert "dod_diff_required" in profile.verification_checks
        assert "dod_must_change_files" in profile.verification_checks

    def test_analysis_gate_no_dod(self, registry):
        profile = registry.get_profile("analysis_gate")
        assert "dod_diff_required" not in profile.verification_checks
        assert "dod_must_change_files" not in profile.verification_checks
