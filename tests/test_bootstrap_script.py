from pathlib import Path


def test_windows_bootstrap_is_ascii_for_windows_powershell_51():
    script = Path("scripts/bootstrap.ps1").read_bytes()

    assert script
    assert all(byte < 128 for byte in script)


def test_windows_bootstrap_targets_repo_local_venv_and_env_file():
    text = Path("scripts/bootstrap.ps1").read_text(encoding="ascii")

    assert 'Join-Path $RepoRoot ".venv"' in text
    assert 'Join-Path $RepoRoot ".env"' in text
    assert 'pip install -e ".[dev,windows-notify]"' in text
    assert 'hikari-resident doctor --env-file' in text
