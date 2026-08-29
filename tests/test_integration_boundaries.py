from __future__ import annotations

import ast
from pathlib import Path
import tomllib


CORE_ROOTS = ("core", "brain", "memory", "personality", "conversation")
FORBIDDEN_IMPORT_PREFIXES = (
    "nonebot",
    "integrations.qq_bridge",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.append(node.module)
    return result


def test_core_packages_do_not_depend_on_qq_or_nonebot():
    repository = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for root_name in CORE_ROOTS:
        for path in (repository / root_name).rglob("*.py"):
            for imported in _imports(path):
                if imported.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{path.relative_to(repository)} -> {imported}")

    assert violations == []


def test_core_install_does_not_require_nonebot():
    repository = Path(__file__).resolve().parents[1]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"].get("dependencies", [])
    normalized = [str(item).lower() for item in dependencies]

    assert not any("nonebot" in item for item in normalized)
    qq_dependencies = [
        str(item).lower()
        for item in project["project"]["optional-dependencies"]["qq"]
    ]
    assert any(item.startswith("nonebot2") for item in qq_dependencies)
    assert any(item.startswith("nonebot-adapter-onebot") for item in qq_dependencies)
