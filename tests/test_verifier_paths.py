"""Tests for canonical path resolution in Verifier."""
import os
import pytest

from src.orchestrator.verifier import Verifier


@pytest.fixture
def repo(tmp_path):
    """Create a mock repo with known file structure."""
    # Runtime paths (canonical)
    (tmp_path / "app" / "finance" / "models.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "finance" / "models.py").write_text("# Invoice model")
    (tmp_path / "app" / "sales" / "models.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "sales" / "models.py").write_text("# Sales model")
    (tmp_path / "app" / "shared" / "fsm.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "shared" / "fsm.py").write_text("# FSM")
    return tmp_path


@pytest.fixture
def verifier(repo):
    """Create a Verifier instance pointing at mock repo."""
    config = {"verify": {}}
    v = Verifier(str(repo), config)
    return v


class TestCanonicalPathResolution:
    def test_existing_path_unchanged(self, verifier):
        """File that exists at given path → canonical."""
        resolved, classification = verifier._resolve_canonical_path("app/finance/models.py")
        assert classification == "canonical"
        assert resolved == "app/finance/models.py"

    def test_basename_resolution(self, verifier):
        """src/models/invoice.py doesn't exist but basename matches elsewhere."""
        # models.py exists in app/finance/ and app/sales/
        resolved, classification = verifier._resolve_canonical_path("src/models/models.py")
        assert classification == "LEGACY_ARTIFACT_MISMATCH"
        assert resolved is not None
        assert "models.py" in resolved

    def test_nonexistent_false_positive(self, verifier):
        """Completely non-existent file → VERIFY_FALSE_POSITIVE."""
        resolved, classification = verifier._resolve_canonical_path("src/nonexistent/xyz_abc.py")
        assert classification == "VERIFY_FALSE_POSITIVE"
        assert resolved is None

    def test_legacy_artifact_mismatch(self, verifier):
        """File doesn't exist at path but basename found elsewhere."""
        resolved, classification = verifier._resolve_canonical_path("src/shared/fsm.py")
        assert classification == "LEGACY_ARTIFACT_MISMATCH"
        assert resolved == "app/shared/fsm.py"

    def test_module_hint_preference(self, verifier):
        """Module hints should influence path preference."""
        verifier.set_module_hints(["finance"])
        resolved, classification = verifier._resolve_canonical_path("src/legacy/models.py")
        assert classification == "LEGACY_ARTIFACT_MISMATCH"
        # Should prefer finance over sales due to module hint
        assert resolved == "app/finance/models.py"

    def test_empty_path(self, verifier):
        """Empty path → VERIFY_FALSE_POSITIVE."""
        resolved, classification = verifier._resolve_canonical_path("")
        assert classification == "VERIFY_FALSE_POSITIVE"
        assert resolved is None
