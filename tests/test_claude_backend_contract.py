from __future__ import annotations

from io import StringIO
import json
from pathlib import Path

import pytest

from engineering.backend import ClaudeEngineeringBackend


def _run(monkeypatch, tmp_path, payloads, *, returncode=0, session_id=None, sink=None):
    captured = {}

    class Input(StringIO):
        def close(self):
            captured["prompt"] = self.getvalue()
            super().close()

    class Process:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs["cwd"]
            self.stdin = Input()
            self.stdout = StringIO("".join(json.dumps(payload) + "\n" for payload in payloads))
            self.stderr = StringIO()

        def wait(self, timeout=None):
            return returncode

    monkeypatch.setattr("engineering.backend.shutil.which", lambda _: "claude")
    monkeypatch.setattr("engineering.backend.subprocess.Popen", Process)
    backend = ClaudeEngineeringBackend(session_id=session_id, event_sink=sink)
    result = backend.run(tmp_path, "Check the existing installation example; fix it only if it is wrong.")
    return backend, result, captured


def _terminal(report, *, subtype="success", is_error=False):
    return {
        "type": "result", "subtype": subtype, "is_error": is_error,
        "session_id": "claude-resumable-session", "result": "CLI completed its response",
        "structured_output": report,
    }


def test_invocation_requires_shared_schema_and_reports_grounded_completed_result(tmp_path: Path, monkeypatch):
    observed = []
    report = {"status": "completed", "summary": "Corrected the installation example.", "validation": ["Copied the command and checked its exit status: 0"]}
    _, result, captured = _run(monkeypatch, tmp_path, [_terminal(report)], sink=observed.append)
    argv = captured["argv"]
    expected = json.loads((Path(__file__).parents[1] / "engineering" / "backend_result.schema.json").read_text(encoding="utf-8"))
    assert json.loads(argv[argv.index("--json-schema") + 1]) == expected
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert captured["cwd"] == tmp_path
    assert "A refusal, proposed plan, or successful reply alone" in captured["prompt"]
    assert result.returncode == 0
    assert result.final_message == report["summary"]
    assert [(event.kind, event.summary) for event in result.events if event.kind == "validation"] == [("validation", report["validation"][0])]
    assert observed == list(result.events)


@pytest.mark.parametrize("status,expected_code", [("blocked", 77), ("failed", 1)])
def test_successful_cli_process_does_not_complete_an_unfinished_task(tmp_path: Path, monkeypatch, status, expected_code):
    report = {"status": status, "summary": "The requested edit was not performed because workspace access was unavailable.", "validation": []}
    backend, result, _ = _run(monkeypatch, tmp_path, [_terminal(report)])
    assert result.returncode == expected_code
    assert f"[claude-code:{status}]" in result.stderr
    assert result.final_message == report["summary"]
    assert result.session_id == backend.session_id == "claude-resumable-session"


def test_legitimate_already_satisfied_task_can_complete_without_edits(tmp_path: Path, monkeypatch):
    report = {"status": "completed", "summary": "The installation example is already correct; no edit was needed.", "validation": ["Ran the existing example successfully"]}
    _, result, _ = _run(monkeypatch, tmp_path, [_terminal(report)])
    assert result.returncode == 0
    assert "no edit was needed" in result.final_message
    assert any(event.kind == "validation" for event in result.events)


@pytest.mark.parametrize("prose", ["Done", "I cannot edit this repository.", '{"status":"completed","summary":"Done","validation":[]}'])
def test_legacy_free_form_success_is_unverified_and_fails_closed(tmp_path: Path, monkeypatch, prose):
    legacy = {"type": "result", "subtype": "success", "session_id": "legacy-session", "is_error": False, "result": prose}
    backend, result, _ = _run(monkeypatch, tmp_path, [legacy])
    assert result.returncode == 1
    assert "[claude-code:invalid_result]" in result.stderr
    assert result.final_message == ""
    assert result.session_id == backend.session_id == "legacy-session"


@pytest.mark.parametrize("report", [
    None,
    "not an object",
    {"status": "completed", "summary": "Done"},
    {"status": "completed", "summary": " ", "validation": []},
    {"status": "completed", "summary": 123, "validation": []},
    {"status": "pending", "summary": "Later", "validation": []},
    {"status": ["completed"], "summary": "Done", "validation": []},
    {"status": "completed", "summary": "Done", "validation": "passed"},
    {"status": "completed", "summary": "Done", "validation": [123]},
    {"status": "completed", "summary": "Done", "validation": [], "unexpected": True},
])
def test_structured_output_must_satisfy_the_whole_result_contract(tmp_path: Path, monkeypatch, report):
    _, result, _ = _run(monkeypatch, tmp_path, [_terminal(report)])
    assert result.returncode == 1
    assert "[claude-code:invalid_result]" in result.stderr
    assert not any(event.kind == "validation" for event in result.events)


@pytest.mark.parametrize("process_code,subtype,is_error", [(3, "success", False), (0, "error_max_turns", True), (0, "", False), (0, "error_max_structured_output_retries", True)])
def test_completed_report_cannot_override_failed_transport(tmp_path: Path, monkeypatch, process_code, subtype, is_error):
    report = {"status": "completed", "summary": "Claimed complete", "validation": ["unsupported result"]}
    _, result, _ = _run(monkeypatch, tmp_path, [_terminal(report, subtype=subtype, is_error=is_error)], returncode=process_code)
    assert result.returncode == (process_code or 1)
    assert not any(event.kind == "validation" for event in result.events)


def test_resume_stays_explicit_and_is_retained_for_blocked_outcomes(tmp_path: Path, monkeypatch):
    report = {"status": "blocked", "summary": "Editing is unavailable", "validation": []}
    backend, result, captured = _run(monkeypatch, tmp_path, [_terminal(report)], session_id="previous-session")
    argv = captured["argv"]
    assert argv[argv.index("--resume") + 1] == "previous-session"
    assert backend.session_id == result.session_id == "claude-resumable-session"
    assert result.returncode == 77


def test_missing_final_event_cannot_complete_from_an_assistant_report(tmp_path: Path, monkeypatch):
    payload = {"type": "assistant", "session_id": "partial-session", "message": {"content": [{"type": "text", "text": json.dumps({"status": "completed", "summary": "Done", "validation": []})}]}}
    _, result, _ = _run(monkeypatch, tmp_path, [payload])
    assert result.returncode == 1
    assert "[claude-code:missing_result]" in result.stderr
    assert result.session_id == "partial-session"


def test_validation_sink_failure_does_not_change_authoritative_task_result(tmp_path: Path, monkeypatch):
    def failing_sink(event):
        raise RuntimeError("observer unavailable")

    report = {"status": "completed", "summary": "Checked example", "validation": ["Existing command passed"]}
    _, result, _ = _run(monkeypatch, tmp_path, [_terminal(report)], sink=failing_sink)
    assert result.returncode == 0
    assert any(event.kind == "validation" for event in result.events)
