from pathlib import Path
import subprocess

from events.sensors import GitSensor, GitSensorError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hikari Test")
    _git(repo, "config", "user.email", "hikari@example.invalid")

    (repo / "note.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-m", "Initial observation")


def test_git_sensor_emits_event_when_head_changes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    sensor = GitSensor(repo)
    assert sensor.name == "git"
    assert sensor.poll() == []

    (repo / "note.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-m", "Second observation")

    events = sensor.poll()

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "git.commit"
    assert event.source == "git"
    assert event.content == "Second observation"
    assert event.context["sha"] == _git(repo, "rev-parse", "HEAD")
    assert event.context["previous_sha"]
    assert event.context["repository"] == str(repo.resolve())
    assert sensor.poll() == []


def test_git_sensor_rejects_non_repository(tmp_path):
    plain_directory = tmp_path / "plain"
    plain_directory.mkdir()

    try:
        GitSensor(plain_directory)
    except GitSensorError:
        pass
    else:
        raise AssertionError("GitSensor should reject a non-Git directory")
