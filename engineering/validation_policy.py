from __future__ import annotations

from collections.abc import Sequence
import re


_README_ONLY_HINT = re.compile(r"\breadme(?:\.md)?\b", re.IGNORECASE)
_BROADER_SCOPE_HINT = re.compile(
    r"\b(code|implementation|module|package|dependency|test|tests|ci|workflow|config)\b",
    re.IGNORECASE,
)
_VALIDATION_CHANGE_ALLOWED = re.compile(
    r"\b(test|tests|pytest|validation|ci|workflow|skip|xfail|marker)\b",
    re.IGNORECASE,
)
_VALIDATION_WEAKENING = re.compile(
    r"pytest\.(?:skip|importorskip|xfail)|pytest\.mark\.(?:skip|skipif|xfail)|"
    r"requires_subprocess|(?:--ignore|--deselect|addopts|filterwarnings)\b",
    re.IGNORECASE,
)


def change_policy_violations(
    intent: str,
    changed_files: Sequence[str],
    diff_text: str,
) -> tuple[str, ...]:
    """Return deterministic scope or validation-integrity violations."""

    violations: list[str] = []
    normalized = tuple(path.replace("\\", "/") for path in changed_files)
    if _README_ONLY_HINT.search(intent) and not _BROADER_SCOPE_HINT.search(intent):
        outside_readme = tuple(
            path for path in normalized if path.rsplit("/", 1)[-1].lower() != "readme.md"
        )
        if outside_readme:
            violations.append(
                "README-only intent changed files outside README.md: "
                + ", ".join(outside_readme)
            )

    added_lines = "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    if _VALIDATION_WEAKENING.search(added_lines) and not _VALIDATION_CHANGE_ALLOWED.search(intent):
        violations.append(
            "change weakens or bypasses validation without explicit validation scope"
        )
    return tuple(violations)
