import json
import os
from pathlib import Path
import sys
import subprocess
import tomllib

import pytest

from engineering.codex_backend import (
    CodexEngineeringBackend, CodexSandboxBoundaryError, codex_provider_configuration,
    codex_permission_profile, _private_worktree_temp, _workspace_credential_denies,
)
from engineering.config import EngineeringBackendConfig


def test_existing_provider_token_is_transferred_without_command_line_exposure(tmp_path: Path):
    (tmp_path / "config.toml").write_text('''model = "configured-model"
model_provider = "custom"
[model_providers.custom]
name = "Custom"
base_url = "https://provider.example/v1"
wire_api = "responses"
experimental_bearer_token = "test-only-token"
requires_openai_auth = false
''', encoding="utf-8")
    args, environment, model = codex_provider_configuration({"CODEX_HOME": str(tmp_path)})
    assert model == "configured-model"
    assert "test-only-token" not in " ".join(args)
    assert environment["HIKARI_CODEX_RUNTIME_TOKEN"] == "test-only-token"
    assert 'model_providers.hikari_engineering.env_key="HIKARI_CODEX_RUNTIME_TOKEN"' in args


def test_nonstandard_provider_credential_name_is_parent_only(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('''model_provider = "custom"
[model_providers.custom]
name = "Custom"
base_url = "https://provider.example/v1"
wire_api = "responses"
env_key = "SESSION_AUTH"
requires_openai_auth = false
''', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SESSION_AUTH", "synthetic-parent-credential")
    backend = CodexEngineeringBackend(executable=sys.executable)
    argv, environment = backend.build_invocation(tmp_path)
    filters = tomllib.loads(next(item for item in argv if item.startswith("shell_environment_policy.filters=")))["shell_environment_policy"]["filters"]
    assert filters["SESSION_AUTH"] == "exclude"
    assert environment["SESSION_AUTH"] == "synthetic-parent-credential"
    assert "synthetic-parent-credential" not in " ".join(argv)
    assert 'shell_environment_policy.inherit="core"' in argv


def test_codex_selection_does_not_inherit_claude_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("HIKARI_ENGINEERING_BACKEND", "codex")
    monkeypatch.setenv("HIKARI_ENGINEERING_MODEL", "sonnet")
    config = EngineeringBackendConfig.from_mapping(dict(os.environ))
    assert config.backend == "codex" and config.executable == "codex"
    backend = CodexEngineeringBackend(executable=sys.executable, writable=True)
    argv, _ = backend.build_invocation(tmp_path)
    assert "sonnet" not in argv
    assert 'default_permissions="hikari-engineering"' in argv
    profile_arg = next(item for item in argv if item.startswith("permissions.hikari-engineering="))
    profile = tomllib.loads(profile_arg)["permissions"]["hikari-engineering"]
    assert profile["extends"] == ":workspace"
    assert profile["filesystem"][":root"] == "deny"
    assert profile["filesystem"][":minimal"] == "read"
    assert profile["filesystem"][":workspace_roots"]["."] == "write"
    assert profile["filesystem"][str(tmp_path / "config.toml")] == "deny"
    assert profile["network"] == {"enabled": False}
    assert 'features.apps=false' in argv and 'web_search="disabled"' in argv
    filters = next(item for item in argv if item.startswith("shell_environment_policy.filters="))
    assert tomllib.loads(filters)["shell_environment_policy"]["filters"]["*TOKEN*"] == "exclude"
    assert not any(item.startswith("shell_environment_policy.exclude=") for item in argv)
    assert "--sandbox" not in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert 'approval_policy="never"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("completed,failed,expected", [(True, False, 0), (False, False, 1), (True, True, 1)])
def test_real_subprocess_jsonl_requires_successful_completion(tmp_path: Path, monkeypatch, completed, failed, expected):
    trace = [
        {"type": "thread.started", "thread_id": "01234567-1234-1234-1234-012345678901"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "python -V", "status": "completed", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"status": "completed", "summary": "verified result", "validation": ["python -V passed"]})}},
    ]
    if completed:
        trace.append({"type": "turn.completed", "usage": {}})
    if failed:
        trace.append({"type": "turn.failed", "error": {"message": "test failure"}})
    command = tmp_path / "recorded_cli.py"
    command.write_text("import sys\nsys.stdin.read()\n" + "\n".join(f"print({json.dumps(json.dumps(item))})" for item in trace), encoding="utf-8")
    backend = CodexEngineeringBackend()
    monkeypatch.setattr(backend, "build_invocation", lambda _, **kwargs: ([sys.executable, str(command)], dict(os.environ)))
    monkeypatch.setattr(backend, "_sandbox_preflight", lambda *args: None)
    observed = []
    backend.set_event_sink(observed.append)
    result = backend.run(tmp_path, "inspect")
    assert result.returncode == expected
    assert result.session_id == "codex:01234567-1234-1234-1234-012345678901"
    if expected == 0:
        assert result.final_message == "verified result"
    assert any("python -V" in event.summary for event in observed)


def test_resume_uses_only_explicit_codex_identity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    backend = CodexEngineeringBackend(executable=sys.executable, session_id="claude-session")
    argv, _ = backend.build_invocation(tmp_path)
    assert "resume" not in argv
    backend = CodexEngineeringBackend(executable=sys.executable, session_id="codex:01234567-1234-1234-1234-012345678901")
    argv, _ = backend.build_invocation(tmp_path)
    assert argv[-3:] == ["resume", "01234567-1234-1234-1234-012345678901", "-"]


def test_completed_codex_process_can_still_report_blocked_task(tmp_path: Path, monkeypatch):
    trace = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
            "status": "blocked", "summary": "Workspace was read-only; no requested file exists", "validation": [],
        })}},
        {"type": "turn.completed"},
    ]
    command = tmp_path / "blocked_cli.py"
    command.write_text("import sys\nsys.stdin.read()\n" + "\n".join(f"print({json.dumps(json.dumps(item))})" for item in trace), encoding="utf-8")
    backend = CodexEngineeringBackend()
    monkeypatch.setattr(backend, "build_invocation", lambda _, **kwargs: ([sys.executable, str(command)], dict(os.environ)))
    monkeypatch.setattr(backend, "_sandbox_preflight", lambda *args: None)
    result = backend.run(tmp_path, "create a file")
    assert result.returncode == 77
    assert "[codex:blocked]" in result.stderr


def test_desktop_session_permissions_are_not_inherited(tmp_path: Path):
    _, environment, _ = codex_provider_configuration({
        "CODEX_HOME": str(tmp_path), "CODEX_PERMISSION_PROFILE": ":danger-full-access",
        "CODEX_THREAD_ID": "parent", "CODEX_APP_TOOLS_PIPE_PATH": "parent-pipe",
    })
    assert environment == {"CODEX_HOME": str(tmp_path)}


@pytest.mark.parametrize("mode", ["unelevated", "elevated"])
def test_sandbox_mode_is_explicit_operator_configuration(mode):
    config = EngineeringBackendConfig.from_mapping({"HIKARI_ENGINEERING_BACKEND": "codex",
                                                     "HIKARI_ENGINEERING_CODEX_SANDBOX": mode})
    assert config.codex_sandbox == mode
    assert config.apply_to_environment({})["HIKARI_ENGINEERING_CODEX_SANDBOX"] == mode
    assert EngineeringBackendConfig.from_mapping({}).codex_sandbox == "unelevated"


@pytest.mark.parametrize("mode", ["", "disabled", "auto", "danger-full-access"])
def test_unsupported_sandbox_modes_do_not_silently_expand(mode):
    with pytest.raises(ValueError, match="CODEX_SANDBOX"):
        EngineeringBackendConfig.from_mapping({"HIKARI_ENGINEERING_CODEX_SANDBOX": mode})


def test_read_only_custom_profile_cannot_gain_workspace_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "auth-store"))
    backend = CodexEngineeringBackend(executable=sys.executable, writable=False)
    argv, _ = backend.build_invocation(tmp_path)
    profile = tomllib.loads(next(item for item in argv if item.startswith("permissions.hikari-engineering=")))["permissions"]["hikari-engineering"]
    assert profile["extends"] == ":read-only"
    assert profile["filesystem"][":workspace_roots"]["."] == "read"
    assert profile["filesystem"][":tmpdir"] == "deny" and profile["filesystem"][":slash_tmp"] == "deny"
    assert profile["filesystem"][str(Path.home() / ".ssh")] == "deny"
    assert profile["filesystem"][str(tmp_path / "auth-store" / "auth.json")] == "deny"
    if os.name == "nt":
        assert 'windows.sandbox="unelevated"' in argv


def test_git_metadata_read_roots_are_trusted_resolved_paths_only(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    def git(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()
    git(repository, "init")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("fixture", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "fixture baseline")
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "-b", "fixture", str(worktree))
    profile = codex_permission_profile(worktree, executable=Path(sys.executable), writable=True,
                                       environment={"CODEX_HOME": str(tmp_path / "credential-store")})
    paths = profile["filesystem"]
    assert paths[str((repository / ".git").resolve())] == "read"
    assert paths[str((repository / ".git" / "worktrees" / "worktree").resolve())] == "read"
    assert str(repository.resolve()) not in paths
    assert paths[":workspace_roots"][".git"] == "read"


def test_unsupported_profile_blocks_before_model_process_and_keeps_resume_identity(tmp_path, monkeypatch):
    backend = CodexEngineeringBackend(session_id="codex:01234567-1234-1234-1234-012345678901")
    invocation = ["codex", "exec", "-c", 'permissions.hikari-engineering={extends=":workspace"}',
                  "-c", 'windows.sandbox="unelevated"']
    monkeypatch.setattr(backend, "build_invocation", lambda _, **kwargs: (invocation, {}))
    observed = []
    def refused(command, *args):
        observed.append(command)
        return subprocess.CompletedProcess(command, 1, "", "windows sandbox failed: Restricted read-only access requires the elevated Windows sandbox backend")
    monkeypatch.setattr(backend, "_run_sandbox_probe", refused)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("model process must not start"))
    result = backend.run(tmp_path, "must not execute this task")
    assert result.returncode == 77 and "[codex:blocked]" in result.stderr
    assert "No model task was executed" in result.stderr
    assert "no broader fallback" in result.stderr
    assert result.session_id == backend.session_id
    assert len(observed) == 1 and observed[0][1] == "sandbox"
    assert observed[0][observed[0].index("-P") + 1] == "hikari-engineering"
    assert "--include-managed-config" in observed[0]
    assert "must not execute this task" not in " ".join(observed[0])
    assert not list(tmp_path.glob(".hikari-tmp-*"))


def test_successful_preflight_requires_owned_marker_and_same_profile(tmp_path, monkeypatch):
    backend = CodexEngineeringBackend()
    argv = ["codex", "exec", "-c", 'permissions.hikari-engineering={extends=":read-only"}',
            "-c", 'windows.sandbox="unelevated"']
    observed = []
    def succeeded(command, *args):
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "HIKARI_ENGINEERING_RESTRICTED_SANDBOX_READY\n", "")
    monkeypatch.setattr(backend, "_run_sandbox_probe", succeeded)
    assert backend._sandbox_preflight(tmp_path, argv, {}) is None
    assert argv[3] in observed[0] and argv[5] in observed[0]
    assert observed[0][-3] == "-B"
    monkeypatch.setattr(backend, "_run_sandbox_probe", lambda command, *args: subprocess.CompletedProcess(command, 0, "unexpected output", ""))
    assert backend._sandbox_preflight(tmp_path, argv, {}).returncode == 77


def test_preflight_timeout_does_not_fall_back_or_run_model(tmp_path, monkeypatch):
    backend = CodexEngineeringBackend(timeout_seconds=1)
    def expired(command, *args):
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(backend, "_run_sandbox_probe", expired)
    result = backend._sandbox_preflight(tmp_path, ["codex", "exec"], {})
    assert result.returncode == 77 and "TimeoutExpired" in result.stderr


def test_preflight_timeout_terminates_its_owned_process_tree(tmp_path, monkeypatch):
    backend = CodexEngineeringBackend(timeout_seconds=1)
    calls = []
    class Process:
        pid = 123
        def communicate(self, timeout):
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("probe", timeout)
            return "", ""
    process = Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(backend, "_kill_owned_tree", lambda proc: calls.append(("kill_tree", proc.pid)))
    with pytest.raises(subprocess.TimeoutExpired):
        backend._run_sandbox_probe(["codex", "sandbox"], tmp_path, {})
    assert calls == [("communicate", 1), ("kill_tree", 123), ("communicate", 10)]


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout.strip()


def _directory_link(link, target):
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, check=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_only_tracked_safe_dotenv_templates_remain_accessible(tmp_path):
    _git(tmp_path, "init")
    for relative in (".env.example", ".env.sample", ".env.template", "nested/.env.example",
                     ".env", ".env.local", "nested/service.env", "untracked/.env.example"):
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text("SYNTHETIC_SETTING=fixture\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n.env.local\n", encoding="utf-8")
    _git(tmp_path, "add", ".env.example", ".env.sample", ".env.template", "nested/.env.example", ".gitignore")
    denied = _workspace_credential_denies(tmp_path)
    assert {path.relative_to(tmp_path).as_posix() for path in denied} == {
        ".env", ".env.local", "nested/service.env", "untracked/.env.example"}
    profile = codex_permission_profile(tmp_path, executable=Path(sys.executable), writable=True,
                                       environment={"CODEX_HOME": str(tmp_path / "credential-store")})
    assert str(tmp_path / ".env.example") not in profile["filesystem"]
    assert str(tmp_path / ".env") in profile["filesystem"]
    assert all("**" not in key for key in profile["filesystem"][":workspace_roots"])


def test_credential_scan_denies_link_without_reading_target(tmp_path, monkeypatch):
    root, outside = tmp_path / "worktree", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / ".env").write_text("synthetic marker", encoding="utf-8")
    link = root / "linked-directory"
    _directory_link(link, outside)
    scanned = []
    real_scan = os.scandir
    def tracked_scan(path):
        scanned.append(Path(path))
        return real_scan(path)
    monkeypatch.setattr(os, "scandir", tracked_scan)
    assert link in _workspace_credential_denies(root)
    assert outside not in scanned and link not in scanned


def test_credential_scan_refuses_incomplete_depth_and_entry_bounds(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".env").write_text("synthetic marker", encoding="utf-8")
    with pytest.raises(CodexSandboxBoundaryError, match="depth"):
        _workspace_credential_denies(tmp_path, max_depth=0)
    with pytest.raises(CodexSandboxBoundaryError, match="entry"):
        _workspace_credential_denies(tmp_path, max_entries=0)


def test_credential_scan_refuses_unreadable_directories(tmp_path, monkeypatch):
    def refused(path):
        raise PermissionError("synthetic fixture permission refusal")
    monkeypatch.setattr(os, "scandir", refused)
    with pytest.raises(CodexSandboxBoundaryError, match="every entry"):
        _workspace_credential_denies(tmp_path)


def test_inspection_profile_allows_only_its_private_temp_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "credential-store"))
    backend = CodexEngineeringBackend(executable=sys.executable, writable=False)
    with _private_worktree_temp(tmp_path) as temporary:
        argv, environment = backend.build_invocation(tmp_path, temporary=temporary)
        assert all(environment[key] == str(temporary) for key in ("TEMP", "TMP", "TMPDIR"))
        configured = tomllib.loads(next(arg for arg in argv if arg.startswith("shell_environment_policy.set=")))["shell_environment_policy"]["set"]
        assert {key: configured[key] for key in ("TEMP", "TMP", "TMPDIR")} == {key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")}
        assert set(configured) <= {"TEMP", "TMP", "TMPDIR", "PATH", "HIKARI_RUNTIME_PYTHON", "VIRTUAL_ENV"}
        profile = tomllib.loads(next(arg for arg in argv if arg.startswith("permissions.hikari-engineering=")))["permissions"]["hikari-engineering"]
        assert profile["filesystem"][":workspace_roots"]["."] == "read"
        assert profile["filesystem"][str(temporary)] == "write"
        assert profile["filesystem"][":tmpdir"] == "write"
        assert profile["filesystem"][":slash_tmp"] == "deny"
    assert not temporary.exists()


def _fake_cli_invocation(script, temporary):
    environment = dict(os.environ)
    environment.update({key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")})
    return [sys.executable, "-B", str(script)], environment


def test_model_success_cleans_temp_git_fixtures_without_touching_source(tmp_path, monkeypatch):
    command = tmp_path / "recorded_temp_cli.py"
    trace = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
        "status": "completed", "summary": "temporary fixture passed", "validation": ["private tempfile created"]})}},
        {"type": "turn.completed"}]
    command.write_text("import sys,tempfile\nfrom pathlib import Path\nsys.stdin.read()\n"
        "folder=Path(tempfile.mkdtemp())\n(folder/'.git').mkdir()\n(folder/'.git'/'config').write_text('fixture')\n"
        "Path('requested-output.txt').write_text('done')\n" +
        "\n".join(f"print({json.dumps(json.dumps(item))})" for item in trace), encoding="utf-8")
    untouched = tmp_path / ".hikari-tmp-preexisting"
    untouched.mkdir()
    (untouched / "keep.txt").write_text("keep", encoding="utf-8")
    backend = CodexEngineeringBackend()
    monkeypatch.setattr(backend, "build_invocation", lambda root, temporary: _fake_cli_invocation(command, temporary))
    monkeypatch.setattr(backend, "_sandbox_preflight", lambda *args: None)
    result = backend.run(tmp_path, "create output")
    assert result.returncode == 0
    assert (tmp_path / "requested-output.txt").read_text() == "done"
    assert list(tmp_path.glob(".hikari-tmp-*")) == [untouched]
    assert (untouched / "keep.txt").read_text() == "keep"


def test_model_timeout_cleans_its_private_temp(tmp_path, monkeypatch):
    command = tmp_path / "slow_cli.py"
    command.write_text("import sys,tempfile,time\nfrom pathlib import Path\nsys.stdin.read()\n"
        "Path(tempfile.mkdtemp()).joinpath('fixture').write_text('temp')\ntime.sleep(30)\n", encoding="utf-8")
    backend = CodexEngineeringBackend(timeout_seconds=0.3)
    monkeypatch.setattr(backend, "build_invocation", lambda root, temporary: _fake_cli_invocation(command, temporary))
    monkeypatch.setattr(backend, "_sandbox_preflight", lambda *args: None)
    assert backend.run(tmp_path, "bounded task").returncode == 124
    assert not list(tmp_path.glob(".hikari-tmp-*"))


def test_preflight_block_removes_private_temp_and_never_starts_model(tmp_path, monkeypatch):
    backend = CodexEngineeringBackend()
    observed = []
    monkeypatch.setattr(backend, "build_invocation", lambda root, temporary: (["codex", "exec"], {}))
    def refuse(root, argv, environment):
        observed.extend(root.glob(".hikari-tmp-*"))
        return backend._failure(77, "[codex:blocked] fixture unsupported platform")
    monkeypatch.setattr(backend, "_sandbox_preflight", refuse)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("model must not start"))
    assert backend.run(tmp_path, "task").returncode == 77
    assert len(observed) == 1 and not observed[0].exists()


def test_private_temp_cleanup_rejects_external_link_and_blocks_result(tmp_path, monkeypatch):
    root, outside = tmp_path / "worktree", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "keep").write_text("must remain", encoding="utf-8")
    backend = CodexEngineeringBackend()
    def linked_result(root, prompt, temporary):
        _directory_link(temporary / "escape", outside)
        return backend._failure(0, "")
    monkeypatch.setattr(backend, "_run_with_private_temp", linked_result)
    result = backend.run(root, "task")
    assert result.returncode == 77 and "cleanup refused" in result.stderr
    assert (outside / "keep").read_text() == "must remain"


def test_missing_private_temp_does_not_suppress_an_execution_exception(tmp_path):
    with pytest.raises(RuntimeError, match="fixture failure"):
        with _private_worktree_temp(tmp_path) as temporary:
            temporary.rmdir()
            raise RuntimeError("fixture failure")


def test_worker_commits_template_edit_without_any_private_temp_artifacts(tmp_path, monkeypatch):
    from engineering.maintainer import project_maintainer_authority
    from engineering.session import EngineeringSessionStore, EngineeringSessionState, EngineeringTurn
    from engineering.worker import EngineeringWorker
    from engineering.backend import EngineeringAgentResult, EngineeringAgentEvent
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / ".env.example").write_text("EXAMPLE_SETTING=old\n", encoding="utf-8")
    _git(repository, "add", ".env.example")
    _git(repository, "commit", "-m", "synthetic baseline")
    baseline = _git(repository, "rev-parse", "HEAD")
    store = EngineeringSessionStore(tmp_path / "state" / "engineering")
    authority = project_maintainer_authority()
    state = store.create(EngineeringSessionState.create(project_id="hikari", repository=repository,
                                                        authority_ceiling=authority))
    turn = EngineeringTurn.create(intent="Update the tracked .env.example template and test the change",
                                   authority=authority, effect="maintain_project")
    store.enqueue_turn(state.session_id, turn)
    backend = CodexEngineeringBackend(writable=True)
    def completed_stage(root, prompt, temporary):
        assert root / ".env.example" not in _workspace_credential_denies(root)
        temp_git = temporary / "pytest-fixture" / ".git"
        temp_git.mkdir(parents=True)
        (temp_git / "config").write_text("synthetic fixture metadata", encoding="utf-8")
        (root / ".env.example").write_text("EXAMPLE_SETTING=new\n", encoding="utf-8")
        return EngineeringAgentResult(0, "", "", "updated safe template", "synthetic-model",
                                       (EngineeringAgentEvent("validation", "fixture template checked"),))
    monkeypatch.setattr(backend, "_run_with_private_temp", completed_stage)
    outcome = EngineeringWorker(store, backend_factory=lambda *args: backend).run_once()
    assert outcome.status == "completed"
    workspace = Path(store.load(state.session_id).workspace_path)
    assert _git(workspace, "diff", "--name-only", baseline, "HEAD") == ".env.example"
    assert not list(workspace.glob(".hikari-tmp-*"))
    assert not _git(workspace, "status", "--porcelain")
    assert (repository / ".env.example").read_text() == "EXAMPLE_SETTING=old\n"


def test_private_temp_creation_never_reuses_preexisting_owned_shape(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("engineering.codex_backend.uuid4", lambda: SimpleNamespace(hex="a" * 32))
    preexisting = tmp_path / (".hikari-tmp-" + "a" * 32)
    preexisting.mkdir()
    (preexisting / "keep").write_text("keep this", encoding="utf-8")
    result = CodexEngineeringBackend().run(tmp_path, "must not run")
    assert result.returncode == 77
    assert (preexisting / "keep").read_text() == "keep this"


def test_command_environment_uses_the_host_validated_python(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-config"))
    backend = CodexEngineeringBackend(executable=sys.executable)
    argv, environment = backend.build_invocation(tmp_path)
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).resolve().parent)
    assert environment["HIKARI_RUNTIME_PYTHON"] == str(Path(sys.executable).resolve())
    import tomllib
    setting = next(item for item in argv if item.startswith("shell_environment_policy.set="))
    values = tomllib.loads(setting)["shell_environment_policy"]["set"]
    assert values["HIKARI_RUNTIME_PYTHON"] == environment["HIKARI_RUNTIME_PYTHON"]
    assert values["PATH"] == environment["PATH"]
    if sys.prefix != sys.base_prefix:
        assert values["VIRTUAL_ENV"] == str(Path(sys.prefix).resolve())
