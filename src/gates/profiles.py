"""
Agent Mesh v2.0 — Gate Profiles
Pre-defined gate profiles for different task types.

Each profile defines which checks to run at each stage:
- input_checks:        validate task inputs before execution
- format_checks:       validate output format (reserved for future)
- rule_checks:         deterministic rule enforcement
- verification_checks: post-execution verification
- escalation_checks:   flag tasks needing human/senior review
"""

from __future__ import annotations

from ..models.task import GateProfile


# ── Profile Definitions ──

CODING_BASIC = GateProfile(
    name="coding_basic",
    input_checks=["target_files_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_secret_leak"],
    verification_checks=["diff_not_empty"],
    escalation_checks=[],
)

API_BASIC = GateProfile(
    name="api_basic",
    input_checks=["target_files_defined", "acceptance_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_secret_leak"],
    verification_checks=["diff_not_empty", "build_pass"],
    escalation_checks=[],
)

CRITICAL_BACKEND = GateProfile(
    name="critical_backend",
    input_checks=["target_files_defined", "acceptance_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_new_dependency", "no_secret_leak"],
    verification_checks=["diff_not_empty", "build_pass", "tests_pass"],
    escalation_checks=["auth_or_payment_touched"],
)

SCHEMA_CRITICAL = GateProfile(
    name="schema_critical",
    input_checks=["target_files_defined", "acceptance_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_secret_leak"],
    verification_checks=["diff_not_empty", "build_pass"],
    escalation_checks=["migration_detected"],
)

INTEGRATION_BASIC = GateProfile(
    name="integration_basic",
    input_checks=["target_files_defined", "acceptance_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_secret_leak"],
    verification_checks=["diff_not_empty", "build_pass", "tests_pass"],
    escalation_checks=[],
)

UI_OPERABILITY_BASIC = GateProfile(
    name="ui_operability_basic",
    input_checks=["target_files_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_secret_leak"],
    verification_checks=["diff_not_empty", "build_pass"],
    escalation_checks=[],
)

PLAYWRIGHT_INFRA_BASIC = GateProfile(
    name="playwright_infra_basic",
    input_checks=["target_files_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only"],
    verification_checks=["diff_not_empty"],
    escalation_checks=[],
)

E2E_SMOKE_GATE = GateProfile(
    name="e2e_smoke_gate",
    input_checks=["target_files_defined", "acceptance_defined"],
    format_checks=[],
    rule_checks=["allowed_paths_only", "no_secret_leak"],
    verification_checks=["diff_not_empty", "build_pass", "tests_pass"],
    escalation_checks=[],
)


# ── All profiles indexed by name ──

ALL_PROFILES: dict[str, GateProfile] = {
    "coding_basic": CODING_BASIC,
    "api_basic": API_BASIC,
    "critical_backend": CRITICAL_BACKEND,
    "schema_critical": SCHEMA_CRITICAL,
    "integration_basic": INTEGRATION_BASIC,
    "ui_operability_basic": UI_OPERABILITY_BASIC,
    "playwright_infra_basic": PLAYWRIGHT_INFRA_BASIC,
    "e2e_smoke_gate": E2E_SMOKE_GATE,
}
