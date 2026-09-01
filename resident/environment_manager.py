from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ENVIRONMENT_RECORD_VERSION = 1
CURRENT_POINTER_VERSION = 1
DEFAULT_EXTRAS = ("dev", "windows-notify")


class EnvironmentManagerError(RuntimeError):
    """Raised when a candidate environment cannot be built, verified, or promoted."""


@dataclass(frozen=True, slots=True)
class CandidateEnvironment:
    environment_id: str
    path: str
    lock_hash: str
    python_version: str
    extras: tuple[str, ...]
    status: str
    created_at: float
    verified_at: float | None = None
    test_returncode: int | None = None

    def to_mapping(self) -> dict[str, object]:
        payload = asdict(self)
        payload["version"] = ENVIRONMENT_RECORD_VERSION
        payload["extras"] = list(self.extras)
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "CandidateEnvironment":
        if payload.get("version") != ENVIRONMENT_RECORD_VERSION:
            raise EnvironmentManagerError("unsupported candidate environment record")
        extras = payload.get("extras")
        if not isinstance(extras, list):
            raise EnvironmentManagerError("candidate environment extras must be a list")
        return cls(
            environment_id=str(payload.get("environment_id", "")),
            path=str(payload.get("path", "")),
            lock_hash=str(payload.get("lock_hash", "")),
            python_version=str(payload.get("python_version", "")),
            extras=tuple(str(item) for item in extras),
            status=str(payload.get("status", "")),
            created_at=float(payload.get("created_at", 0.0)),
            verified_at=(
                float(payload["verified_at"])
                if payload.get("verified_at") is not None
                else None
            ),
            test_returncode=(
                int(payload["test_returncode"])
                if payload.get("test_returncode") is not None
                else None
            ),
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


class EnvironmentManager:
    """Build and verify immutable candidate environments beside the live runtime.

    The manager never rewrites the currently running interpreter. Promotion only
    updates a small durable pointer; the stable launcher can consume that pointer
    during a controlled Resident restart.
    """

    def __init__(
        self,
        repository: str | Path,
        state_dir: str | Path,
        *,
        uv_executable: str = "uv",
        runner: Runner = subprocess.run,
    ) -> None:
        self.repository = Path(repository).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.root = self.state_dir / "environments"
        self.uv_executable = uv_executable
        self._runner = runner
        if not (self.repository / "pyproject.toml").is_file():
            raise ValueError("environment repository requires pyproject.toml")

    @property
    def pointer_path(self) -> Path:
        return self.root / "current.json"

    def record_path(self, environment_id: str) -> Path:
        return self.root / "records" / f"{environment_id}.json"

    def log_dir(self, environment_id: str) -> Path:
        return self.root / "logs" / environment_id

    def lock_hash(self) -> str:
        lock_path = self.repository / "uv.lock"
        if not lock_path.is_file():
            raise EnvironmentManagerError("uv.lock is missing")
        return sha256(lock_path.read_bytes()).hexdigest()

    def candidate_for(
        self,
        *,
        extras: Sequence[str] = DEFAULT_EXTRAS,
        python_version: str | None = None,
    ) -> CandidateEnvironment:
        normalized_extras = tuple(sorted({str(item).strip() for item in extras if str(item).strip()}))
        version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        lock_hash = self.lock_hash()
        identity = "\n".join((lock_hash, version, *normalized_extras))
        environment_id = sha256(identity.encode("utf-8")).hexdigest()[:20]
        path = self.root / environment_id
        return CandidateEnvironment(
            environment_id=environment_id,
            path=str(path),
            lock_hash=lock_hash,
            python_version=version,
            extras=normalized_extras,
            status="planned",
            created_at=time.time(),
        )

    def build(
        self,
        *,
        extras: Sequence[str] = DEFAULT_EXTRAS,
        timeout_seconds: float = 900.0,
    ) -> CandidateEnvironment:
        candidate = self.candidate_for(extras=extras)
        executable = shutil.which(self.uv_executable)
        if executable is None:
            raise EnvironmentManagerError("uv executable was not found")
        path = Path(candidate.path)
        building = self._replace(candidate, status="building")
        self._save(building)

        argv = [executable, "sync", "--locked"]
        for extra in candidate.extras:
            argv.extend(("--extra", extra))
        environment = os.environ.copy()
        environment["UV_PROJECT_ENVIRONMENT"] = str(path)
        result = self._runner(
            argv,
            cwd=self.repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
        self._write_log(self.log_dir(candidate.environment_id) / "build.log", result)
        if result.returncode != 0:
            failed = self._replace(building, status="build_failed")
            self._save(failed)
            raise EnvironmentManagerError("candidate environment build failed")
        python = self.python_path(path)
        if not python.is_file():
            failed = self._replace(building, status="build_failed")
            self._save(failed)
            raise EnvironmentManagerError("candidate environment has no Python executable")
        built = self._replace(building, status="built")
        self._save(built)
        return built

    def validate(
        self,
        environment_id: str,
        *,
        timeout_seconds: float = 600.0,
    ) -> CandidateEnvironment:
        candidate = self.load(environment_id)
        if candidate.status not in {"built", "validation_failed", "verified"}:
            raise EnvironmentManagerError("candidate environment is not ready for validation")
        path = Path(candidate.path)
        python = self.python_path(path)
        if not python.is_file():
            raise EnvironmentManagerError("candidate Python executable is missing")

        probe = self._runner(
            [str(python), "-c", _NESTED_PROCESS_PROBE],
            cwd=self.repository,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
        )
        self._write_log(self.log_dir(candidate.environment_id) / "process-probe.log", probe)
        if probe.returncode != 0:
            failed = self._replace(
                candidate,
                status="validation_environment_failed",
                verified_at=time.time(),
                test_returncode=probe.returncode,
            )
            self._save(failed)
            raise EnvironmentManagerError("candidate nested process probe failed")

        tests = self._runner(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=self.repository,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
        self._write_log(self.log_dir(candidate.environment_id) / "pytest.log", tests)
        status = "verified" if tests.returncode == 0 else "validation_failed"
        verified = self._replace(
            candidate,
            status=status,
            verified_at=time.time(),
            test_returncode=tests.returncode,
        )
        self._save(verified)
        if tests.returncode != 0:
            raise EnvironmentManagerError("candidate project tests failed")
        return verified

    def promote(self, environment_id: str) -> dict[str, object]:
        candidate = self.load(environment_id)
        if candidate.status != "verified" or candidate.test_returncode != 0:
            raise EnvironmentManagerError("only a verified candidate may be promoted")
        current = self.current()
        payload: dict[str, object] = {
            "version": CURRENT_POINTER_VERSION,
            "environment_id": candidate.environment_id,
            "path": candidate.path,
            "lock_hash": candidate.lock_hash,
            "promoted_at": time.time(),
            "previous_environment_id": (
                current.get("environment_id") if current is not None else None
            ),
            "previous_path": current.get("path") if current is not None else None,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_json(self.pointer_path, payload)
        return payload

    def rollback(self) -> dict[str, object]:
        current = self.current()
        if current is None:
            raise EnvironmentManagerError("no promoted environment is available for rollback")
        if not current.get("previous_environment_id") or not current.get("previous_path"):
            payload: dict[str, object] = {
                "version": CURRENT_POINTER_VERSION,
                "environment_id": None,
                "path": None,
                "rolled_back_at": time.time(),
                "previous_environment_id": current.get("environment_id"),
                "previous_path": current.get("path"),
            }
            self.pointer_path.unlink(missing_ok=True)
            return payload
        previous = self.load(str(current["previous_environment_id"]))
        if previous.status != "verified":
            raise EnvironmentManagerError("previous environment is no longer verified")
        payload = {
            "version": CURRENT_POINTER_VERSION,
            "environment_id": previous.environment_id,
            "path": previous.path,
            "lock_hash": previous.lock_hash,
            "promoted_at": time.time(),
            "previous_environment_id": current.get("environment_id"),
            "previous_path": current.get("path"),
        }
        self._atomic_json(self.pointer_path, payload)
        return payload

    def current(self) -> dict[str, object] | None:
        if not self.pointer_path.is_file():
            return None
        try:
            payload = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentManagerError("current environment pointer is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("version") != CURRENT_POINTER_VERSION:
            raise EnvironmentManagerError("current environment pointer is invalid")
        return payload

    def current_python(self, fallback: str | Path) -> Path:
        """Select the verified promoted interpreter, or the stable bootstrap one."""

        current = self.current()
        if current is None:
            return Path(fallback).expanduser().resolve()
        environment_id = str(current.get("environment_id", "")).strip()
        if not environment_id:
            raise EnvironmentManagerError("current environment pointer has no environment id")
        candidate = self.load(environment_id)
        if candidate.status != "verified" or candidate.test_returncode != 0:
            raise EnvironmentManagerError("promoted environment is not verified")
        if Path(candidate.path).resolve() != Path(str(current.get("path", ""))).resolve():
            raise EnvironmentManagerError("current environment pointer path does not match its record")
        python = self.python_path(Path(candidate.path))
        if not python.is_file():
            raise EnvironmentManagerError("promoted environment Python is missing")
        return python.resolve()

    def load(self, environment_id: str) -> CandidateEnvironment:
        path = self.record_path(environment_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvironmentManagerError(f"candidate environment is unreadable: {environment_id}") from exc
        if not isinstance(payload, dict):
            raise EnvironmentManagerError("candidate environment record must be an object")
        return CandidateEnvironment.from_mapping(payload)

    @staticmethod
    def python_path(root: Path) -> Path:
        if os.name == "nt":
            return root / "Scripts" / "python.exe"
        return root / "bin" / "python"

    @staticmethod
    def _replace(candidate: CandidateEnvironment, **changes: object) -> CandidateEnvironment:
        payload = asdict(candidate)
        payload.update(changes)
        return CandidateEnvironment(**payload)

    def _save(self, candidate: CandidateEnvironment) -> None:
        self._atomic_json(self.record_path(candidate.environment_id), candidate.to_mapping())

    @staticmethod
    def _write_log(path: Path, result: subprocess.CompletedProcess[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            f"returncode={result.returncode}\n"
            f"--- stdout ---\n{result.stdout or ''}\n"
            f"--- stderr ---\n{result.stderr or ''}\n"
        )
        path.write_text(content, encoding="utf-8", newline="\n")

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)


_NESTED_PROCESS_PROBE = (
    "import subprocess,sys; "
    "result=subprocess.run([sys.executable,'-c','raise SystemExit(0)'],"
    "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE); "
    "raise SystemExit(result.returncode)"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Hikari candidate runtime environments")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    sub.add_parser("build")
    validate = sub.add_parser("validate")
    validate.add_argument("environment_id")
    promote = sub.add_parser("promote")
    promote.add_argument("environment_id")
    sub.add_parser("status")
    sub.add_parser("rollback")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from resident.paths import default_state_dir

    args = build_parser().parse_args(argv)
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    manager = EnvironmentManager(args.repo, state_dir)
    try:
        if args.command == "plan":
            payload = manager.candidate_for().to_mapping()
        elif args.command == "build":
            payload = manager.build().to_mapping()
        elif args.command == "validate":
            payload = manager.validate(args.environment_id).to_mapping()
        elif args.command == "promote":
            payload = manager.promote(args.environment_id)
        elif args.command == "status":
            payload = manager.current() or {}
        elif args.command == "rollback":
            payload = manager.rollback()
        else:
            return 2
    except EnvironmentManagerError as exc:
        print(str(exc))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
