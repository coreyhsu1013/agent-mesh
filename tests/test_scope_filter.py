"""Tests for ScopeFilter."""
import pytest
from src.orchestrator.scope_filter import ScopeFilter, ScopeFilterResult
from src.orchestrator.verifier import VerifyReport, VerifyIssue


@pytest.fixture
def config():
    return {"verify": {"exclude_modules": []}}


@pytest.fixture
def scope_filter(config, tmp_path):
    return ScopeFilter(config, str(tmp_path))


class TestScopeFilter:
    def test_no_chunk_id_passes_all(self, scope_filter):
        """With empty chunk_id, all issues pass through."""
        report = VerifyReport(cycle=1, issues=[
            VerifyIssue(category="spec_gap", severity="HIGH", message="gap 1", module="auth"),
            VerifyIssue(category="spec_gap", severity="HIGH", message="gap 2", module="payment"),
        ])
        # Empty chunk_id — _get_chunk_scope_modules returns empty set
        result = scope_filter.filter(report, chunk_id="")
        assert len(result.executable) == 2
        assert len(result.out_of_scope) == 0

    def test_chunk_scoping(self, scope_filter):
        """Issues matching chunk scope are executable, others deferred."""
        report = VerifyReport(cycle=1, issues=[
            VerifyIssue(category="spec_gap", severity="HIGH", message="gap 1", module="Auth"),
            VerifyIssue(category="spec_gap", severity="HIGH", message="gap 2", module="Payment"),
            VerifyIssue(category="build", severity="HIGH", message="build error"),
        ])
        result = scope_filter.filter(report, chunk_id="chunk-1-auth-backend")
        # "auth" is in scope, "payment" is not, build always passes
        assert len(result.executable) == 2  # auth gap + build
        assert len(result.out_of_scope) == 1  # payment gap

    def test_non_gap_always_executable(self, scope_filter):
        """Build, test, conflict issues are always in scope."""
        report = VerifyReport(cycle=1, issues=[
            VerifyIssue(category="build", severity="HIGH", message="build fail"),
            VerifyIssue(category="test", severity="HIGH", message="test fail"),
            VerifyIssue(category="conflict", severity="HIGH", message="conflict"),
        ])
        result = scope_filter.filter(report, chunk_id="chunk-1-auth")
        assert len(result.executable) == 3
        assert len(result.out_of_scope) == 0

    def test_no_module_conservative(self, scope_filter):
        """Issues without module info are kept (conservative)."""
        report = VerifyReport(cycle=1, issues=[
            VerifyIssue(category="spec_gap", severity="HIGH", message="something"),
        ])
        result = scope_filter.filter(report, chunk_id="chunk-1-auth")
        assert len(result.executable) == 1

    def test_exclude_modules(self, tmp_path):
        """Issues from excluded modules are false positives."""
        config = {"verify": {"exclude_modules": ["legacy"]}}
        sf = ScopeFilter(config, str(tmp_path))
        report = VerifyReport(cycle=1, issues=[
            VerifyIssue(category="spec_gap", severity="HIGH", message="gap", module="legacy-module"),
            VerifyIssue(category="spec_gap", severity="HIGH", message="gap", module="auth"),
        ])
        result = sf.filter(report, chunk_id="chunk-1-auth")
        assert len(result.false_positives) == 1
        assert len(result.executable) == 1


class TestChunkScopeModules:
    def test_parse_chunk_id(self, scope_filter):
        modules = scope_filter._get_chunk_scope_modules("chunk-3-notification-backend")
        assert "notification" in modules
        assert "backend" not in modules

    def test_parse_simple_chunk(self, scope_filter):
        modules = scope_filter._get_chunk_scope_modules("chunk-1-auth")
        assert "auth" in modules

    def test_multi_module_chunk(self, scope_filter):
        modules = scope_filter._get_chunk_scope_modules("chunk-2-product-inventory")
        assert "product" in modules
        assert "inventory" in modules

    def test_with_spec_file(self, tmp_path):
        """Reads scope from spec file header."""
        sf = ScopeFilter({"verify": {}}, str(tmp_path))
        mesh_dir = tmp_path / ".agent-mesh"
        mesh_dir.mkdir()
        (mesh_dir / "chunk-1-auth-spec.md").write_text(
            "# Chunk 1: Auth Module\nScope: authentication, authorization\n"
        )
        modules = sf._get_chunk_scope_modules("chunk-1-auth")
        assert "auth" in modules
        assert "authentication" in modules
        assert "authorization" in modules
