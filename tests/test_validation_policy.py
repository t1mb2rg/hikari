from __future__ import annotations

from engineering.validation_policy import change_policy_violations


def test_readme_only_intent_rejects_test_infrastructure_changes() -> None:
    violations = change_policy_violations(
        "在 README 的 M7-07 章节末尾添加一句中文说明。",
        ("README.md", "tests/conftest.py", "pyproject.toml"),
        "+pytest.mark.skipif(True, reason='sandbox')\n+requires_subprocess = True\n",
    )

    assert any("README-only" in item for item in violations)
    assert any("weakens or bypasses validation" in item for item in violations)


def test_validation_weakening_requires_explicit_validation_scope() -> None:
    violations = change_policy_violations(
        "Implement the new API response.",
        ("tests/test_api.py",),
        "+pytest.skip('not available here')\n",
    )

    assert violations == (
        "change weakens or bypasses validation without explicit validation scope",
    )


def test_explicit_test_task_can_change_validation_behavior() -> None:
    violations = change_policy_violations(
        "Update pytest validation for platforms without subprocess support.",
        ("tests/conftest.py",),
        "+pytest.mark.skipif(no_subprocess, reason='unsupported')\n",
    )

    assert violations == ()
