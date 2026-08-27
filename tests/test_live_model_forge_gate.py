from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from actions import ActionExecutionError, ActionPlanningError

GATE_PATH = Path(__file__).resolve().parents[1] / "examples" / "live_model_forge_gate.py"

# Goal chosen by the fake model; it appears nowhere in the gate source, so any
# dispatch carrying it proves the planner's parsed proposal was used.
MODEL_GOAL = "Add one missing docstring to the smallest module, decided by the model."

MODEL_RESPONSE = {
    "decision": "propose",
    "action": "run_forge_task",
    "arguments": {
        "project_id": "hikari",
        "goal": MODEL_GOAL,
        "constraints": ["Do not change public behavior."],
        "acceptance": ["The full hikari test suite passes."],
    },
    "effect": "dispatch one bounded engineering task to Forge",
    "reason": "the live gate needs one verified tiny self-improvement",
    "confidence": 0.93,
}


def _load_gate():
    spec = importlib.util.spec_from_file_location("hikari_live_model_forge_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return self.response


class _RecordingRunner:
    """Records argv plus the task YAML content at dispatch time, before the
    adapter's best-effort cleanup removes the temporary file."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.yaml_by_call: list[str] = []
        self.returncode = returncode

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        self.yaml_by_call.append(Path(argv[2]).read_text(encoding="utf-8"))
        return self.returncode


# --- happy path --------------------------------------------------------------


def test_gate_dispatches_only_after_confirmation_and_uses_model_proposal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    gate = _load_gate()
    provider = _FakeProvider(json.dumps(MODEL_RESPONSE))
    runner = _RecordingRunner()
    confirmations = 0

    # The confirmation callback proves ordering: at the moment the human says
    # yes, Forge must not have been invoked even once.
    def confirm() -> bool:
        nonlocal confirmations
        confirmations += 1
        assert runner.calls == []
        return True

    exit_code = gate.run_gate(
        provider,
        confirm=confirm,
        runner=runner,
        work_dir=tmp_path / "tasks",
    )

    assert exit_code == 0
    assert provider.calls == 1
    assert confirmations == 1
    assert len(runner.calls) == 1

    # Real CLI contract, built from the trusted profile only.
    argv = runner.calls[0]
    assert argv[:2] == ["forge", "run"]
    assert argv[argv.index("--repo") + 1] == str(gate.REPOSITORY)
    assert argv[argv.index("--backend") + 1] == "claude"
    assert argv[argv.index("--max-attempts") + 1] == "3"
    assert argv[argv.index("--claude-permission-mode") + 1] == "auto"
    assert argv[argv.index("--claude-max-turns") + 1] == "30"

    # The dispatched task carries the model-parsed proposal text, not any
    # gate-authored copy of it.
    parsed = yaml.safe_load(runner.yaml_by_call[0])
    assert parsed["goal"] == MODEL_GOAL
    assert parsed["verification"] == ["python -m pytest -q"]

    out = capsys.readouterr().out
    for label in (
        "action:",
        "project_id:",
        "goal:",
        "constraints:",
        "acceptance:",
        "effect:",
        "reason:",
        "confidence:",
        "risk:",
    ):
        assert f"  {label}" in out
    assert "run_forge_task" in out
    assert "hikari" in out
    assert MODEL_GOAL in out
    assert "confidence:  0.93" in out
    assert "reversible" in out
    assert "No Forge process has started yet." in out


# --- model declines ----------------------------------------------------------


def test_none_decision_exits_cleanly_and_never_touches_forge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    gate = _load_gate()
    provider = _FakeProvider('{"decision":"none","reason":"No useful allowed action."}')
    runner = _RecordingRunner()

    def confirm() -> bool:
        raise AssertionError("gate must not ask for confirmation when the model declines")

    exit_code = gate.run_gate(
        provider,
        confirm=confirm,
        runner=runner,
        work_dir=tmp_path / "tasks",
    )

    assert exit_code == 0
    assert provider.calls == 1
    assert runner.calls == []
    assert "no action" in capsys.readouterr().out
    assert not (tmp_path / "tasks").exists()


# --- confirmation boundary ---------------------------------------------------


def test_denied_confirmation_never_invokes_forge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    gate = _load_gate()
    provider = _FakeProvider(json.dumps(MODEL_RESPONSE))
    runner = _RecordingRunner()

    exit_code = gate.run_gate(
        provider,
        confirm=lambda: False,
        runner=runner,
        work_dir=tmp_path / "tasks",
    )

    assert exit_code == 0
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "[authorization] deny" in out
    assert not (tmp_path / "tasks").exists()


# --- invalid model output keeps failing loudly -------------------------------


def test_unregistered_action_failure_is_preserved(tmp_path: Path):
    gate = _load_gate()
    provider = _FakeProvider(json.dumps({**MODEL_RESPONSE, "action": "run_shell"}))
    runner = _RecordingRunner()

    with pytest.raises(ActionPlanningError, match="unregistered action"):
        gate.run_gate(
            provider,
            confirm=lambda: True,
            runner=runner,
            work_dir=tmp_path / "tasks",
        )

    assert runner.calls == []
    assert not (tmp_path / "tasks").exists()


def test_model_supplied_trusted_settings_are_rejected_before_forge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    gate = _load_gate()
    arguments = {**MODEL_RESPONSE["arguments"], "backend": "codex", "verification": ["evil.sh"]}
    provider = _FakeProvider(json.dumps({**MODEL_RESPONSE, "arguments": arguments}))
    runner = _RecordingRunner()

    exit_code = gate.run_gate(
        provider,
        confirm=lambda: True,
        runner=runner,
        work_dir=tmp_path / "tasks",
    )

    assert exit_code == 1
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "[execution] dispatch failed" in out
    assert "rejected:" in out


def test_forge_start_failure_is_reported_without_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    gate = _load_gate()
    provider = _FakeProvider(json.dumps(MODEL_RESPONSE))

    def failing_runner(argv: list[str]) -> int:
        raise OSError("no such executable")

    exit_code = gate.run_gate(
        provider,
        confirm=lambda: True,
        runner=failing_runner,
        work_dir=tmp_path / "tasks",
    )

    assert exit_code == 1
    assert "[execution] dispatch failed" in capsys.readouterr().out


# --- the gate never authors the proposal itself ------------------------------


def test_gate_source_never_constructs_an_action_proposal():
    """The physical gate must derive every proposal from the real model
    response parsed by ModelActionPlanner. A manual ActionProposal anywhere in
    the gate source would break that boundary."""

    source = GATE_PATH.read_text(encoding="utf-8")

    assert "ActionProposal" not in source
    assert "ModelActionPlanner(" in source
    assert "forge_task_action_spec()" in source
