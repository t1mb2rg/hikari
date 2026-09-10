from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from resident.environment_manager import EnvironmentManager, EnvironmentManagerError, _PYTHON_VERSION_PROBE


def _repo(path: Path):
    path.mkdir()
    (path / "pyproject.toml").write_text("[project]\nname='example'\nversion='0.1'\n", encoding="utf-8")
    (path / "uv.lock").write_text("version=1\n", encoding="utf-8")
    (path / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    return path


def _manager(tmp_path, monkeypatch, *, repository=None, state_dir=None, hook=None):
    repository = repository or _repo(tmp_path / "repo")
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if "sync" in argv:
            python = EnvironmentManager.python_path(Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]))
            python.parent.mkdir(parents=True)
            python.write_text("fake interpreter", encoding="utf-8")
        if hook:
            hook(argv)
        output = f"{sys.version_info.major}.{sys.version_info.minor}" if len(argv) > 2 and argv[2] == _PYTHON_VERSION_PROBE else "verified"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr("resident.environment_manager.shutil.which", lambda _: "fake-uv")
    return EnvironmentManager(repository, state_dir or tmp_path / "state", runner=runner), calls


def test_identity_binds_content_and_checkout_even_when_lockfile_is_identical(tmp_path: Path):
    one = EnvironmentManager(_repo(tmp_path / "one"), tmp_path / "state")
    two = EnvironmentManager(_repo(tmp_path / "two"), tmp_path / "state")
    original = one.candidate_for()
    assert two.candidate_for().environment_id != original.environment_id
    (one.repository / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = one.candidate_for()
    assert changed.lock_hash == original.lock_hash
    assert changed.environment_id != original.environment_id
    assert changed.source_fingerprint != original.source_fingerprint
    assert changed.source_path == str(one.repository)


def test_build_installs_non_editable_and_does_not_resync_existing_candidate(tmp_path: Path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch)
    built = manager.build()
    assert "--no-editable" in calls[0]
    count = len(calls)
    assert manager.build() == built
    assert len(calls) == count


def test_promoted_candidate_cannot_be_rebuilt_or_revalidated(tmp_path: Path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch)
    candidate = manager.validate(manager.build().environment_id)
    manager.promote(candidate.environment_id)
    record = manager.record_path(candidate.environment_id).read_bytes()
    pointer = manager.pointer_path.read_bytes()
    before = len(calls)
    with pytest.raises(EnvironmentManagerError, match="cannot be rebuilt"):
        manager.build()
    with pytest.raises(EnvironmentManagerError, match="cannot be revalidated"):
        manager.validate(candidate.environment_id)
    assert len(calls) == before
    assert manager.record_path(candidate.environment_id).read_bytes() == record
    assert manager.pointer_path.read_bytes() == pointer


def test_current_interpreter_is_protected_even_without_promotion(tmp_path: Path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch)
    candidate = manager.build()
    monkeypatch.setattr(sys, "executable", str(manager.python_path(Path(candidate.path))))
    before = len(calls)
    with pytest.raises(EnvironmentManagerError, match="running"):
        manager.validate(candidate.environment_id)
    assert len(calls) == before


def test_source_drift_blocks_validation_and_stale_promotion(tmp_path: Path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch)
    candidate = manager.build()
    (manager.repository / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    before = len(calls)
    with pytest.raises(EnvironmentManagerError, match="source checkout changed"):
        manager.validate(candidate.environment_id)
    assert len(calls) == before
    (manager.repository / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    manager.validate(candidate.environment_id)
    (manager.repository / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(EnvironmentManagerError, match="source checkout changed"):
        manager.promote(candidate.environment_id)
    assert manager.current() is None


def test_source_changes_during_tests_never_receive_verified_status(tmp_path: Path, monkeypatch):
    repository = _repo(tmp_path / "repo")

    def drift(argv):
        if "pytest" in argv:
            (repository / "module.py").write_text("VALUE = 9\n", encoding="utf-8")

    manager, _ = _manager(tmp_path, monkeypatch, repository=repository, hook=drift)
    candidate = manager.build()
    with pytest.raises(EnvironmentManagerError, match="source checkout changed"):
        manager.validate(candidate.environment_id)
    assert manager.load(candidate.environment_id).status == "built"
    assert manager.load(candidate.environment_id).verified_at is None


def test_source_changes_during_build_do_not_produce_a_built_candidate(tmp_path: Path, monkeypatch):
    repository = _repo(tmp_path / "repo")

    def drift(argv):
        if "sync" in argv:
            (repository / "module.py").write_text("VALUE = 8\n", encoding="utf-8")

    manager, _ = _manager(tmp_path, monkeypatch, repository=repository, hook=drift)
    planned = manager.candidate_for()
    with pytest.raises(EnvironmentManagerError, match="source checkout changed"):
        manager.build()
    assert manager.load(planned.environment_id).status == "building"


def test_existing_partial_path_is_never_overwritten(tmp_path: Path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch)
    candidate = manager.candidate_for()
    existing = Path(candidate.path)
    existing.mkdir(parents=True)
    sentinel = existing / "do-not-touch"
    sentinel.write_text("existing state", encoding="utf-8")
    with pytest.raises(EnvironmentManagerError, match="already exists"):
        manager.build()
    assert calls == []
    assert sentinel.read_text(encoding="utf-8") == "existing state"


def test_legacy_current_pointer_still_selects_interpreter_but_cannot_be_revalidated(tmp_path: Path):
    manager = EnvironmentManager(_repo(tmp_path / "repo"), tmp_path / "state")
    legacy_id = "a" * 20
    legacy = replace(manager.candidate_for(), environment_id=legacy_id, path=str(manager.root / legacy_id), status="verified", verified_at=1, test_returncode=0)
    python = manager.python_path(Path(legacy.path))
    python.parent.mkdir(parents=True)
    python.write_text("legacy interpreter", encoding="utf-8")
    record = legacy.to_mapping()
    for key in ("source_path", "source_fingerprint", "source_revision", "promoted_at"):
        record.pop(key)
    manager._atomic_json(manager.record_path(legacy_id), record)
    pointer = {"version": 1, "environment_id": legacy_id, "path": legacy.path}
    manager._atomic_json(manager.pointer_path, pointer)
    assert manager.current_python(tmp_path / "fallback") == python.resolve()
    assert manager.load(legacy_id).source_fingerprint is None
    with pytest.raises(EnvironmentManagerError, match="legacy candidate"):
        manager.promote(legacy_id)
    assert manager.current() == pointer


def test_records_cannot_retarget_a_managed_environment_or_another_checkout(tmp_path: Path, monkeypatch):
    manager, calls = _manager(tmp_path, monkeypatch)
    candidate = manager.build()
    other = EnvironmentManager(_repo(tmp_path / "other"), manager.state_dir, runner=manager._runner)
    before = len(calls)
    with pytest.raises(EnvironmentManagerError, match="different source checkout"):
        other.validate(candidate.environment_id)
    assert len(calls) == before
    manager._atomic_json(manager.record_path(candidate.environment_id), {**candidate.to_mapping(), "path": str(manager.repository / ".venv")})
    with pytest.raises(EnvironmentManagerError, match="managed path"):
        manager.load(candidate.environment_id)
    with pytest.raises(EnvironmentManagerError, match="identity"):
        manager.load("../outside")


def test_promotion_is_idempotent_and_does_not_erase_rollback_target(tmp_path: Path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    first = manager.validate(manager.build(extras=("dev",)).environment_id)
    manager.promote(first.environment_id)
    second = manager.validate(manager.build(extras=()).environment_id)
    current = manager.promote(second.environment_id)
    assert current["previous_environment_id"] == first.environment_id
    assert manager.promote(second.environment_id) == current
    rolled_back = manager.rollback()
    assert rolled_back["environment_id"] == first.environment_id
    assert rolled_back["rollback_scope"] == "interpreter_only"
    assert rolled_back["source_restored"] is False
    assert manager.load(second.environment_id).promoted_at is not None
    with pytest.raises(EnvironmentManagerError, match="cannot be rebuilt"):
        manager.build(extras=())


def test_runtime_state_inside_checkout_does_not_change_source_identity(tmp_path: Path, monkeypatch):
    repository = _repo(tmp_path / "repo")
    manager, _ = _manager(tmp_path, monkeypatch, repository=repository, state_dir=repository / "state")
    before = manager.candidate_for()
    candidate = manager.build()
    assert candidate.environment_id == before.environment_id
    assert manager.candidate_for().environment_id == before.environment_id


def test_git_source_binding_tracks_revision_dirty_and_untracked_source(tmp_path: Path):
    repository = _repo(tmp_path / "repo")
    def git(*args):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    git("init")
    git("config", "user.name", "Environment Tests")
    git("config", "user.email", "test@example.invalid")
    (repository / ".gitignore").write_text("ignored.cache\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial")
    manager = EnvironmentManager(repository, tmp_path / "state")
    initial = manager.candidate_for()
    assert initial.source_revision and len(initial.source_revision) == 40
    (repository / "ignored.cache").write_text("ignored generated output", encoding="utf-8")
    assert manager.candidate_for().environment_id == initial.environment_id
    (repository / "new.py").write_text("NEW = True\n", encoding="utf-8")
    assert manager.candidate_for().environment_id != initial.environment_id
    (repository / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert manager.candidate_for().source_fingerprint != initial.source_fingerprint


def test_operation_lock_prevents_another_manager_from_rewriting_build_state(tmp_path: Path, monkeypatch):
    repository = _repo(tmp_path / "repo")
    state = tmp_path / "state"
    blocked = []

    def nested(argv):
        if "sync" in argv:
            other = EnvironmentManager(repository, state)
            with pytest.raises(EnvironmentManagerError, match="another environment operation"):
                other.build()
            blocked.append(True)

    manager, _ = _manager(tmp_path, monkeypatch, repository=repository, state_dir=state, hook=nested)
    assert manager.build().status == "built"
    assert blocked == [True]
    assert not (manager.root / ".operation.lock").exists()
