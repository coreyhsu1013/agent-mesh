"""Tests for TaskNormalizer."""
import pytest
from src.models.task import Task
from src.orchestrator.task_normalizer import TaskNormalizer


@pytest.fixture
def normalizer():
    return TaskNormalizer()


class TestInferTaskType:
    def test_analysis(self, normalizer):
        task = Task(title="Analyze authentication flow", description="Research current auth")
        normalizer.normalize(task)
        assert task.task_type == "analysis"

    def test_schema(self, normalizer):
        task = Task(title="Update prisma schema for products")
        normalizer.normalize(task)
        assert task.task_type == "schema"

    def test_model(self, normalizer):
        task = Task(title="Create pydantic model for Order")
        normalizer.normalize(task)
        assert task.task_type == "model"

    def test_service(self, normalizer):
        task = Task(title="Implement order service business logic")
        normalizer.normalize(task)
        assert task.task_type == "service"

    def test_router(self, normalizer):
        task = Task(title="Add API endpoint for products")
        normalizer.normalize(task)
        assert task.task_type == "router"

    def test_frontend(self, normalizer):
        task = Task(title="Create React component for dashboard")
        normalizer.normalize(task)
        assert task.task_type == "frontend"

    def test_test_only(self, normalizer):
        task = Task(title="Write playwright e2e tests for login")
        normalizer.normalize(task)
        assert task.task_type == "test_only"

    def test_config(self, normalizer):
        task = Task(title="Update yaml settings for deployment")
        normalizer.normalize(task)
        assert task.task_type == "config"

    def test_general_fallback(self, normalizer):
        task = Task(title="Improve performance of sorting algorithm")
        normalizer.normalize(task)
        assert task.task_type == "general"

    def test_scheduler(self, normalizer):
        task = Task(title="Create cron job for daily cleanup")
        normalizer.normalize(task)
        assert task.task_type == "scheduler"

    def test_listener(self, normalizer):
        task = Task(title="Add event handler for order events")
        normalizer.normalize(task)
        assert task.task_type == "listener"


class TestTypeRules:
    def test_analysis_allowed_no_diff(self, normalizer):
        task = Task(title="Research best practices for caching")
        normalizer.normalize(task)
        assert task.allowed_no_diff is True
        assert task.required_target_files == []
        assert task.min_changed_files == 0

    def test_implementation_required_target_files(self, normalizer):
        task = Task(
            title="Add user service",
            target_files=["src/services/user.ts", "src/routes/user.ts"],
        )
        normalizer.normalize(task)
        assert task.allowed_no_diff is False
        assert task.required_target_files == ["src/services/user.ts", "src/routes/user.ts"]
        assert task.min_changed_files >= 1

    def test_no_overwrite_existing_required_target_files(self, normalizer):
        task = Task(
            title="Fix user service",
            target_files=["src/services/user.ts"],
            required_target_files=["src/custom.ts"],
        )
        normalizer.normalize(task)
        assert task.required_target_files == ["src/custom.ts"]


class TestNormalizePlan:
    def test_stats(self, normalizer):
        tasks = [
            Task(title="Analysis of current state"),
            Task(title="Create prisma schema", target_files=["prisma/schema.prisma"]),
            Task(title="Add API endpoint", target_files=["src/routes/api.ts"]),
        ]
        stats = normalizer.normalize_plan(tasks)
        assert stats["total"] == 3
        assert "analysis" in stats["type_distribution"]
        assert "schema" in stats["type_distribution"]
        assert stats["no_target_files"] == 0  # analysis is allowed_no_diff

    def test_chunk_id_propagation(self, normalizer):
        tasks = [Task(title="Fix bug")]
        normalizer.normalize_plan(tasks, chunk_id="chunk-1-auth")
        assert tasks[0].chunk_id == "chunk-1-auth"

    def test_no_overwrite_existing_chunk_id(self, normalizer):
        tasks = [Task(title="Fix bug", chunk_id="existing")]
        normalizer.normalize_plan(tasks, chunk_id="new-chunk")
        assert tasks[0].chunk_id == "existing"

    def test_no_target_files_warning(self, normalizer):
        tasks = [Task(title="Add user module")]  # no target_files, not analysis
        stats = normalizer.normalize_plan(tasks)
        assert stats["no_target_files"] == 1


class TestVerifierScope:
    def test_scope_with_module_and_files(self, normalizer):
        task = Task(
            title="Fix auth",
            module="auth",
            target_files=["src/auth/login.ts"],
            acceptance_criteria="Login works",
        )
        normalizer.normalize(task)
        scope = task.verifier_scope
        assert any("module: auth" in s for s in scope)
        assert any("src/auth/login.ts" in s for s in scope)

    def test_scope_empty_for_minimal_task(self, normalizer):
        task = Task(title="Do something", module="core")
        normalizer.normalize(task)
        # module=core is skipped, no target_files, no criteria
        assert task.verifier_scope == []


class TestInferTargetFiles:
    def test_no_overwrite_existing(self, normalizer):
        """Existing target_files should not be overwritten."""
        task = Task(title="Fix auth", target_files=["src/auth.ts"])
        normalizer.normalize(task)
        assert task.target_files == ["src/auth.ts"]

    def test_promote_required_target_files(self, normalizer):
        """required_target_files should promote to target_files."""
        task = Task(title="Fix auth module", required_target_files=["app/auth/models.py"])
        normalizer.normalize(task)
        assert "app/auth/models.py" in task.target_files

    def test_infer_from_source_gaps(self, normalizer):
        """source_gaps containing file paths should be extracted."""
        task = Task(
            title="Fix sales gaps",
            source_gaps=["sales: Missing CRUD — file: app/sales/models.py"],
        )
        normalizer.normalize(task)
        assert "app/sales/models.py" in task.target_files

    def test_infer_from_description_file_path(self, normalizer):
        """File paths in description should be extracted."""
        task = Task(
            title="Fix finance module",
            description="The file app/finance/models.py is missing the Invoice model",
        )
        normalizer.normalize(task)
        assert "app/finance/models.py" in task.target_files

    def test_infer_from_module_name(self, normalizer, tmp_path):
        """Module name should generate candidate dirs, filtered by existence."""
        # Create a matching directory
        (tmp_path / "app" / "sales").mkdir(parents=True)
        task = Task(title="Fix sales", module="sales")
        normalizer.normalize(task, repo_dir=str(tmp_path))
        assert "app/sales" in task.target_files

    def test_regex_fallback_last(self, normalizer):
        """When only regex can find paths, it should still work."""
        task = Task(
            title="Update lib/utils/format.py with new logic",
            description="No structured paths here",
        )
        normalizer.normalize(task)
        assert "lib/utils/format.py" in task.target_files

    def test_analysis_no_inference(self, normalizer):
        """Analysis tasks should not get target_files inferred."""
        task = Task(title="Analysis of app/sales/models.py file")
        normalizer.normalize(task)
        assert task.task_type == "analysis"
        assert task.target_files == []

    def test_cap_at_8_files(self, normalizer):
        """Inferred target_files should be capped at 8."""
        paths = " ".join(f"app/mod{i}/file.py" for i in range(15))
        task = Task(title="Fix all modules", description=paths)
        normalizer.normalize(task)
        assert len(task.target_files) <= 8

    def test_inference_miss_recorded(self, normalizer):
        """When inference fails, inference_miss should have structured reason."""
        task = Task(title="Do something vague", module="core")
        normalizer.normalize(task)
        assert task.inference_miss.get("reason") == "no_paths_found"

    def test_repo_aware_filtering(self, normalizer, tmp_path):
        """Non-existent paths should be filtered when repo_dir is provided."""
        task = Task(
            title="Fix stuff",
            description="Update app/nonexistent/models.py",
        )
        normalizer.normalize(task, repo_dir=str(tmp_path))
        # Path doesn't exist in tmp_path, should be filtered out
        assert "app/nonexistent/models.py" not in task.target_files

    def test_related_dirs_inferred(self, normalizer, tmp_path):
        """related_dirs should be inferred from target_files with existence check."""
        (tmp_path / "app" / "shared").mkdir(parents=True)
        task = Task(
            title="Fix sales module",
            target_files=["app/sales/models.py"],
        )
        normalizer.normalize(task, repo_dir=str(tmp_path))
        assert "app/shared" in task.related_dirs

    def test_related_dirs_not_speculative(self, normalizer, tmp_path):
        """related_dirs should not contain dirs that don't exist."""
        task = Task(
            title="Fix sales module",
            target_files=["app/sales/models.py"],
        )
        normalizer.normalize(task, repo_dir=str(tmp_path))
        # app/shared doesn't exist, should not be in related_dirs
        assert "app/shared" not in task.related_dirs


class TestBackwardCompat:
    def test_old_plan_no_v21_fields(self):
        """Old plan.json without v2.1 fields should still load."""
        d = {
            "id": "test-1",
            "title": "Old task",
            "complexity": "M",
        }
        task = Task.from_dict(d)
        assert task.chunk_id == ""
        assert task.allowed_no_diff is False
        assert task.required_target_files == []
        assert task.min_changed_files == 0
        assert task.source_gaps == []
        assert task.depends_on == []

    def test_old_must_change_files_migration(self):
        """Old plan.json with must_change_files should migrate to required_target_files."""
        d = {"title": "Old task", "must_change_files": ["a.ts"]}
        task = Task.from_dict(d)
        assert task.required_target_files == ["a.ts"]

    def test_definition_of_done_str_migration(self):
        """Old string definition_of_done should migrate to list."""
        d = {"title": "Old task", "definition_of_done": "Must work"}
        task = Task.from_dict(d)
        assert task.definition_of_done == ["Must work"]

    def test_to_dict_excludes_defaults(self):
        """to_dict should not include v2.1 fields when they're default."""
        task = Task(title="Test")
        d = task.to_dict()
        assert "chunk_id" not in d
        assert "allowed_no_diff" not in d
        assert "required_target_files" not in d
        assert "min_changed_files" not in d

    def test_to_dict_includes_non_defaults(self):
        """to_dict should include v2.1 fields when they're set."""
        task = Task(
            title="Test",
            chunk_id="chunk-1",
            allowed_no_diff=True,
            required_target_files=["a.ts"],
            min_changed_files=1,
        )
        d = task.to_dict()
        assert d["chunk_id"] == "chunk-1"
        assert d["allowed_no_diff"] is True
        assert d["required_target_files"] == ["a.ts"]
        assert d["min_changed_files"] == 1
