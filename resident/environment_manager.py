from __future__ import annotations

from resident.console import configure_utf8_output

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4


ENVIRONMENT_RECORD_VERSION = 1
ENVIRONMENT_ID_VERSION = 3
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
    source_path: str | None = None
    source_fingerprint: str | None = None
    source_revision: str | None = None
    promoted_at: float | None = None

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
            source_path=str(payload["source_path"]) if payload.get("source_path") is not None else None,
            source_fingerprint=str(payload["source_fingerprint"]) if payload.get("source_fingerprint") is not None else None,
            source_revision=str(payload["source_revision"]) if payload.get("source_revision") is not None else None,
            promoted_at=float(payload["promoted_at"]) if payload.get("promoted_at") is not None else None,
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _exclusive_operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._operation_lock():
            return method(self, *args, **kwargs)
    return guarded


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
        self._environment_path(environment_id)
        return self.root / "records" / f"{environment_id}.json"

    def log_dir(self, environment_id: str) -> Path:
        self._environment_path(environment_id)
        return self.root / "logs" / environment_id

    def _environment_path(self, environment_id: str) -> Path:
        if not isinstance(environment_id, str) or re.fullmatch(r"[a-f0-9]{20}", environment_id) is None:
            raise EnvironmentManagerError("invalid candidate environment identity")
        expected = self.root / environment_id
        if self.root.resolve() != self.root or expected.resolve() != expected:
            raise EnvironmentManagerError("candidate environment path must not redirect outside its managed location")
        return expected

    def _source_snapshot(self) -> tuple[str, str | None]:
        """Fingerprint checkout content, including tracked edits and untracked source.

        Git ignore rules exclude local secrets and generated artifacts. Source
        archives without Git use the same conventional build/runtime exclusions.
        Runtime state is always excluded even when located beneath the checkout.
        """
        revision = None
        excluded_state = self.state_dir if self.repository in self.state_dir.parents else self.root
        if (self.repository / ".git").exists():
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
            try:
                listed = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false", "-C", str(self.repository),
                     "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                    capture_output=True, timeout=30, check=False, env=env,
                )
                head = subprocess.run(
                    ["git", "-C", str(self.repository), "rev-parse", "HEAD"],
                    capture_output=True, timeout=15, check=False, env=env,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise EnvironmentManagerError("source checkout identity could not be read") from exc
            if listed.returncode:
                raise EnvironmentManagerError("source checkout file inventory could not be read")
            names = [item.decode("utf-8", errors="surrogateescape") for item in listed.stdout.split(b"\x00") if item]
            paths = [self.repository / name for name in names]
            if head.returncode == 0:
                revision = head.stdout.decode("ascii", errors="strict").strip()
        else:
            excluded = {".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
                        ".ruff_cache", "build", "dist", ".hikari", ".env", ".env.local"}
            paths = []
            for directory, folders, files in os.walk(self.repository):
                parent = Path(directory)
                folders[:] = [name for name in folders if name not in excluded and not name.endswith(".egg-info")
                              and (parent / name).resolve() != excluded_state]
                for name in folders:
                    if (parent / name).is_symlink():
                        raise EnvironmentManagerError("source directories must not be symlinks")
                paths.extend(parent / name for name in files if name not in excluded and not name.endswith((".pyc", ".pyo")))
        digest = sha256()
        digest.update((revision or "source-archive").encode("utf-8") + b"\0")
        for path in sorted(set(paths)):
            if path == excluded_state or excluded_state in path.parents:
                continue
            if self.repository not in path.resolve().parents:
                raise EnvironmentManagerError("source file escapes the selected checkout")
            if path.is_symlink() or path.is_dir():
                raise EnvironmentManagerError("source links and nested repository entries require an explicit source snapshot")
            digest.update(path.relative_to(self.repository).as_posix().encode("utf-8", errors="surrogateescape") + b"\0")
            if not path.exists():
                digest.update(b"deleted\0")
                continue
            try:
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise EnvironmentManagerError("source file could not be fingerprinted") from exc
            digest.update(b"\0")
        return digest.hexdigest(), revision

    def _assert_source(self, candidate: CandidateEnvironment) -> None:
        if not candidate.source_path or not candidate.source_fingerprint:
            raise EnvironmentManagerError("legacy candidate has no source binding; build a fresh candidate")
        if Path(candidate.source_path).resolve() != self.repository:
            raise EnvironmentManagerError("candidate belongs to a different source checkout")
        fingerprint, revision = self._source_snapshot()
        if (fingerprint != candidate.source_fingerprint or revision != candidate.source_revision
                or self.lock_hash() != candidate.lock_hash):
            raise EnvironmentManagerError("source checkout changed since the candidate was built or verified")

    def verify_source(self, environment_id: str) -> CandidateEnvironment:
        """Check source binding before a launcher opts into that candidate's code."""
        candidate = self.load(environment_id)
        self._assert_source(candidate)
        return candidate

    def _was_promoted(self, candidate: CandidateEnvironment) -> bool:
        current = self.current()
        path = Path(candidate.path)
        running = Path(sys.prefix).resolve() == path or path in Path(sys.executable).resolve().parents
        return running or candidate.promoted_at is not None or bool(current and candidate.environment_id in {
            current.get("environment_id"), current.get("previous_environment_id"),
        })

    @contextmanager
    def _operation_lock(self):
        if self.root.resolve() != self.root:
            raise EnvironmentManagerError("managed environment root must not be a redirected path")
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".operation.lock"
        try:
            with lock.open("x", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "created_at": time.time()}, stream)
        except FileExistsError as exc:
            raise EnvironmentManagerError(
                "another environment operation owns this state directory; stale locks require operator review"
            ) from exc
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

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
        fingerprint, revision = self._source_snapshot()
        identity = "\n".join(
            (f"identity-version={ENVIRONMENT_ID_VERSION}", str(self.repository), fingerprint,
             lock_hash, version, *normalized_extras)
        )
        environment_id = sha256(identity.encode("utf-8")).hexdigest()[:20]
        path = self._environment_path(environment_id)
        return CandidateEnvironment(
            environment_id=environment_id,
            path=str(path),
            lock_hash=lock_hash,
            python_version=version,
            extras=normalized_extras,
            status="planned",
            created_at=time.time(),
            source_path=str(self.repository),
            source_fingerprint=fingerprint,
            source_revision=revision,
        )

    @_exclusive_operation
    def build(
        self,
        *,
        extras: Sequence[str] = DEFAULT_EXTRAS,
        timeout_seconds: float = 900.0,
    ) -> CandidateEnvironment:
        candidate = self.candidate_for(extras=extras)
        if self.record_path(candidate.environment_id).exists():
            existing = self.load(candidate.environment_id)
            self._assert_source(existing)
            if self._was_promoted(existing):
                raise EnvironmentManagerError("promoted or running environments cannot be rebuilt")
            if existing.status in {"built", "verified"} and self.python_path(Path(existing.path)).is_file():
                return existing
            raise EnvironmentManagerError("candidate already has build state; it will not be overwritten")
        if Path(candidate.path).exists():
            raise EnvironmentManagerError("candidate path already exists without a trusted build record")
        if self._was_promoted(candidate):
            raise EnvironmentManagerError("promoted or running environments cannot be rebuilt")
        executable = shutil.which(self.uv_executable)
        if executable is None:
            raise EnvironmentManagerError("uv executable was not found")
        path = Path(candidate.path)
        building = self._replace(candidate, status="building")
        self._save(building, create_only=True)

        argv = [
            executable,
            "sync",
            "--locked",
            "--python",
            candidate.python_version,
            "--no-editable",
            "--reinstall-package",
            "hikari",
        ]
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
        version_probe = self._runner(
            [str(python), "-c", _PYTHON_VERSION_PROBE],
            cwd=self.repository,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self._write_log(
            self.log_dir(candidate.environment_id) / "python-version.log",
            version_probe,
        )
        actual_version = (version_probe.stdout or "").strip()
        if version_probe.returncode != 0 or actual_version != candidate.python_version:
            failed = self._replace(building, status="build_failed")
            self._save(failed)
            raise EnvironmentManagerError(
                "candidate Python version mismatch: "
                f"expected {candidate.python_version}, got {actual_version or 'unknown'}"
            )
        built = self._replace(building, status="built")
        self._assert_source(built)
        self._save(built)
        return built

    @_exclusive_operation
    def validate(
        self,
        environment_id: str,
        *,
        timeout_seconds: float = 600.0,
    ) -> CandidateEnvironment:
        candidate = self.load(environment_id)
        if candidate.status not in {"built", "validation_failed", "verified"}:
            raise EnvironmentManagerError("candidate environment is not ready for validation")
        if self._was_promoted(candidate):
            raise EnvironmentManagerError("promoted or running environments cannot be revalidated in place")
        self._assert_source(candidate)
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
            env={
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("HIKARI_")
            },
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
        self._write_log(self.log_dir(candidate.environment_id) / "pytest.log", tests)
        self._assert_source(candidate)
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

    @_exclusive_operation
    def promote(self, environment_id: str) -> dict[str, object]:
        candidate = self.load(environment_id)
        if candidate.status != "verified" or candidate.test_returncode != 0:
            raise EnvironmentManagerError("only a verified candidate may be promoted")
        self._assert_source(candidate)
        if not self.python_path(Path(candidate.path)).is_file():
            raise EnvironmentManagerError("candidate Python executable is missing")
        current = self.current()
        if current is not None and current.get("environment_id") == environment_id:
            return current
        promoted_at = time.time()
        self._save(self._replace(candidate, promoted_at=promoted_at))
        payload: dict[str, object] = {
            "version": CURRENT_POINTER_VERSION,
            "environment_id": candidate.environment_id,
            "path": candidate.path,
            "lock_hash": candidate.lock_hash,
            "promoted_at": promoted_at,
            "source_path": candidate.source_path,
            "source_fingerprint": candidate.source_fingerprint,
            "source_revision": candidate.source_revision,
            "previous_environment_id": (
                current.get("environment_id") if current is not None else None
            ),
            "previous_path": current.get("path") if current is not None else None,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_json(self.pointer_path, payload)
        return payload

    @_exclusive_operation
    def rollback(self) -> dict[str, object]:
        """Select a previous interpreter; this does not restore checkout contents."""
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
                "rollback_scope": "interpreter_only",
                "source_restored": False,
            }
            self.pointer_path.unlink(missing_ok=True)
            return payload
        previous = self.load(str(current["previous_environment_id"]))
        if previous.status != "verified" or previous.test_returncode != 0:
            raise EnvironmentManagerError("previous environment is no longer verified")
        if not self.python_path(Path(previous.path)).is_file():
            raise EnvironmentManagerError("previous environment Python is missing")
        payload = {
            "version": CURRENT_POINTER_VERSION,
            "environment_id": previous.environment_id,
            "path": previous.path,
            "lock_hash": previous.lock_hash,
            "promoted_at": time.time(),
            "previous_environment_id": current.get("environment_id"),
            "previous_path": current.get("path"),
            "source_path": previous.source_path,
            "source_fingerprint": previous.source_fingerprint,
            "source_revision": previous.source_revision,
            "rollback_scope": "interpreter_only",
            "source_restored": False,
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
        identity = payload.get("environment_id")
        path = payload.get("path")
        if not isinstance(identity, str) or not isinstance(path, str):
            raise EnvironmentManagerError("current environment pointer has no valid identity/path")
        if Path(path).expanduser().resolve() != self._environment_path(identity):
            raise EnvironmentManagerError("current environment pointer path is outside its managed location")
        previous = payload.get("previous_environment_id")
        previous_path = payload.get("previous_path")
        if (previous is None) != (previous_path is None):
            raise EnvironmentManagerError("current environment pointer has an incomplete rollback target")
        if previous is not None:
            if not isinstance(previous_path, str) or Path(previous_path).expanduser().resolve() != self._environment_path(previous):
                raise EnvironmentManagerError("rollback target is outside its managed location")
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
        try:
            candidate = CandidateEnvironment.from_mapping(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise EnvironmentManagerError("candidate environment record is invalid") from exc
        expected = self._environment_path(environment_id)
        if candidate.environment_id != environment_id or Path(candidate.path).expanduser().resolve() != expected:
            raise EnvironmentManagerError("candidate record identity or managed path does not match")
        return candidate

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

    def _save(self, candidate: CandidateEnvironment, *, create_only: bool = False) -> None:
        path = self.record_path(candidate.environment_id)
        if create_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as record:
                    json.dump(candidate.to_mapping(), record, ensure_ascii=False, indent=2)
            except FileExistsError as exc:
                raise EnvironmentManagerError("another build already owns this candidate") from exc
        else:
            self._atomic_json(path, candidate.to_mapping())

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
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


_NESTED_PROCESS_PROBE = (
    "import subprocess,sys; "
    "result=subprocess.run([sys.executable,'-c','raise SystemExit(0)'],"
    "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE); "
    "raise SystemExit(result.returncode)"
)

_PYTHON_VERSION_PROBE = (
    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
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
    configure_utf8_output()
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
