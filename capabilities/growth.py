from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from uuid import NAMESPACE_URL, uuid5

from conversation.models import UserTurn
from engineering.maintainer import project_maintainer_authority
from engineering.session import (
    EngineeringProtocolError, EngineeringSessionState, EngineeringSessionStore,
    EngineeringTurn,
)

from .runtime import (
    MAX_BYTES, SERVICE_CONTRACTS, CapabilityError, RecipeRuntime, canonical,
    capability_identity, validate_schema, validate_value,
)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _private(turn: UserTurn) -> dict:
    if not isinstance(turn, UserTurn) or turn.scope != "private":
        raise CapabilityError("capability growth and invocation require a private conversation")
    return {"channel": turn.channel, "conversation_id": turn.conversation_id,
            "actor_id": turn.actor_id, "scope": turn.scope, "text": turn.text}


def _same_owner(source: dict, turn: UserTurn) -> None:
    current = _private(turn)
    for key in ("channel", "conversation_id", "actor_id", "scope"):
        if source[key] != current[key]:
            raise CapabilityError("capability belongs to a different private conversation principal")


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="strict", timeout=15)
    if proc.returncode:
        raise CapabilityError("candidate Git evidence is unavailable: " + proc.stderr.strip()[:500])
    return proc.stdout.strip()


class CapabilityGrowth:
    """Private durable requests, Engineering dispatch, candidate evidence, and invocation.

    Only recipes over the closed pure service set can become callable here. Native
    code is a reviewable implementation candidate; this process never imports it.
    Activation is a trusted operator API, deliberately absent from model tools.
    """

    def __init__(self, path: str | Path, *, engineering_store: EngineeringSessionStore,
                 repository: str | Path, implementation_enabled: bool = False) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engineering_store = engineering_store
        self.repository = Path(repository).expanduser().resolve()
        self.implementation_enabled = implementation_enabled is True
        self.runtime = RecipeRuntime()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS growth_requests (
                  request_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
                  request_json TEXT NOT NULL, status TEXT NOT NULL,
                  attempt INTEGER NOT NULL DEFAULT 1, session_id TEXT, turn_id TEXT,
                  candidate_digest TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
                  result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS growth_events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                  kind TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS growth_candidates (
                  capability_id TEXT NOT NULL, version INTEGER NOT NULL,
                  request_id TEXT NOT NULL, digest TEXT NOT NULL, candidate_json TEXT NOT NULL,
                  PRIMARY KEY(capability_id, version));
                CREATE TABLE IF NOT EXISTS growth_active (
                  capability_id TEXT NOT NULL, version INTEGER NOT NULL,
                  digest TEXT NOT NULL, operator_ref TEXT NOT NULL, activated_at REAL NOT NULL,
                  PRIMARY KEY(capability_id, version));
                CREATE TRIGGER IF NOT EXISTS immutable_growth_request
                BEFORE UPDATE OF request_json,source_key,request_id ON growth_requests
                BEGIN SELECT RAISE(ABORT, 'growth source requests are immutable'); END;
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _get(db, request_id: str) -> dict:
        row = db.execute("SELECT * FROM growth_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise CapabilityError("unknown capability growth request")
        result = json.loads(row["request_json"])
        result.update({key: row[key] for key in (
            "request_id", "status", "attempt", "session_id", "turn_id", "candidate_digest",
            "created_at", "updated_at",
        )})
        result["evidence"] = json.loads(row["evidence_json"])
        result["result"] = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return result

    @staticmethod
    def _event(db, request_id: str, kind: str, evidence: dict) -> None:
        db.execute("INSERT INTO growth_events(request_id,kind,evidence_json,created_at) VALUES(?,?,?,?)",
                   (request_id, kind, canonical(evidence), time.time()))

    def _status(self, db, request_id: str, status: str, evidence: dict) -> None:
        db.execute("UPDATE growth_requests SET status=?,evidence_json=?,updated_at=? WHERE request_id=?",
                   (status, canonical(evidence), time.time(), request_id))
        self._event(db, request_id, status, evidence)

    def request(self, *, source_ref: str, turn: UserTurn, capability_id: str,
                input_schema: dict, output_schema: dict, acceptance_cases: list,
                constraints=(), resume_input: object = None, version: int = 1,
                implementation_kind: str = "recipe") -> dict:
        source = _private(turn)
        if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 500:
            raise CapabilityError("growth requires a stable nonempty source_ref")
        capability_identity(capability_id, version)
        if implementation_kind not in {"recipe", "native"}:
            raise CapabilityError("implementation_kind must be recipe or native")
        validate_schema(input_schema)
        validate_schema(output_schema)
        if not isinstance(constraints, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in constraints
        ):
            raise CapabilityError("constraints must be nonempty text items")
        self._validate_cases(acceptance_cases, input_schema, output_schema)
        if resume_input is not None:
            validate_value(resume_input, input_schema)
        payload = {"source_ref": source_ref, "source_turn": source, "intent": turn.text,
                   "capability_id": capability_id, "version": version,
                   "implementation_kind": implementation_kind,
                   "input_schema": input_schema, "output_schema": output_schema,
                   "constraints": list(constraints), "acceptance_cases": acceptance_cases,
                   "resume_input": resume_input}
        encoded = canonical(payload)
        source_key = _digest([turn.channel, turn.conversation_id, source_ref])
        request_id = uuid5(NAMESPACE_URL, "hikari-growth:" + source_key).hex
        with self._db() as db:
            previous = db.execute("SELECT request_json FROM growth_requests WHERE source_key=?",
                                  (source_key,)).fetchone()
            if previous is not None:
                if previous[0] != encoded:
                    raise CapabilityError("source_ref already owns a different immutable capability request")
                return self._get(db, request_id)
            now = time.time()
            db.execute("INSERT INTO growth_requests(request_id,source_key,request_json,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?)", (request_id, source_key, encoded, "requested", now, now))
            self._event(db, request_id, "requested", {"source_ref": source_ref,
                        "capability_id": capability_id, "implementation_kind": implementation_kind})
            return self._get(db, request_id)

    @staticmethod
    def _validate_cases(cases: object, input_schema: dict, output_schema: dict) -> None:
        if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
            raise CapabilityError("1..64 executable acceptance cases are required")
        for case in cases:
            if not isinstance(case, dict) or set(case) != {"input", "expected"}:
                raise CapabilityError("acceptance cases require input and expected")
            validate_value(case["input"], input_schema)
            validate_value(case["expected"], output_schema)
        canonical(cases)

    @staticmethod
    def candidate_directory(request: dict) -> str:
        return f"capabilities/candidates/{request['capability_id']}/v{request['version']}"

    def _dispatch(self, db, request: dict) -> None:
        rid = request["request_id"]
        if not self.implementation_enabled:
            self._status(db, rid, "blocked", {"reason": "capability implementation is disabled by operator configuration"})
            return
        session_id = "growth-" + rid + "-" + str(request["attempt"])
        turn_id = uuid5(NAMESPACE_URL, "hikari-growth-turn:" + session_id).hex
        authority = project_maintainer_authority()
        directory = self.candidate_directory(request)
        if request["implementation_kind"] == "recipe":
            deliverables = [directory + "/recipe.json", directory + "/tests.json"]
            contract = {
                "format": "hikari.recipe.v1", "capability_id": request["capability_id"],
                "version": request["version"], "owner": "hikari.private", "permissions": [],
                "input_schema": request["input_schema"], "output_schema": request["output_schema"],
                "steps": [{"id": "step_name", "service": "text.lines",
                           "args": {"text": {"ref": "input.text"}}}],
                "return": {"ref": "step_name"},
            }
            format_help = (
                "Create recipe.json using the following format (the example steps/return must be adapted) "
                + canonical(contract) + ". The closed service set and exact argument types are "
                + canonical(SERVICE_CONTRACTS) + ". Expressions use {\"ref\":\"input.field\"} "
                "or {\"ref\":\"earlier_step\"}; literals, arrays, and objects are supported. "
                "No arbitrary Python, shell, imports, permissions, filesystem, network, or hidden state. "
                "tests.json is a JSON array of {input,expected} cases; include edge cases. "
                "Run the trusted capabilities.runtime.RecipeRuntime against both the supplied immutable "
                "acceptance cases and your tests.json. Do not modify the runtime to pass tests."
            )
        else:
            deliverables = [directory + "/manifest.json", directory + "/implementation.py",
                            directory + "/test_implementation.py"]
            format_help = (
                "Create a native candidate with implementation.py exposing invoke(inputs), "
                "test_implementation.py with meaningful tests including the immutable acceptance cases, "
                "and manifest.json with exactly format=hikari.native.v1, owner=hikari.private, "
                "capability_id, version, input_schema, output_schema, entrypoint=implementation.py:invoke, "
                "and permissions (an explicit list of required effect names). "
                "Run its tests in the existing isolated Engineering execution environment. "
                "A native candidate is not activated by Resident: operator validation and deployment "
                "must install a reviewed bounded host adapter. Do not change live configuration."
            )
        context = canonical({"kind": "hikari.capability_growth.v1", "request_id": rid,
                             "source_request": {key: request[key] for key in (
                                 "source_ref", "source_turn", "intent", "constraints",
                                 "acceptance_cases", "input_schema", "output_schema",
                                 "resume_input", "capability_id", "version", "implementation_kind",
                             )}, "allowed_changed_files": deliverables})
        intent = ("Implement and test this missing Hikari capability as an isolated candidate. "
                  "Original intent and all acceptance requirements are immutable in context. "
                  "Only change these files: " + ", ".join(deliverables) + ". " + format_help +
                  " Do not deploy, activate, publish, expand authority, or alter the original request.")
        candidate_turn = EngineeringTurn(turn_id=turn_id, intent=intent, context=context,
                                         authority=authority, created_at=request["created_at"],
                                         effect="maintain_project", source_request_id=rid,
                                         constraints=tuple(request["constraints"]),
                                         acceptance_criteria=tuple(canonical(case) for case in request["acceptance_cases"]))
        # Stable ids recover a crash after session/turn persistence but before SQLite commit.
        session_directory = self.engineering_store.root / session_id
        if session_directory.exists():
            state = self.engineering_store.load(session_id)
            if state.repository != str(self.repository) or state.authority_ceiling != authority:
                raise CapabilityError("growth session identity or authority changed")
        else:
            state = self.engineering_store.create(EngineeringSessionState.create(
                project_id="hikari", repository=self.repository,
                authority_ceiling=authority, session_id=session_id))
        if state.current_turn_id is None:
            self.engineering_store.enqueue_turn(session_id, candidate_turn)
        elif state.current_turn_id != turn_id:
            raise CapabilityError("growth session owns a different engineering turn")
        else:
            existing = self.engineering_store.load_turn(session_id, turn_id)
            if not self._same_persisted_turn(existing, candidate_turn):
                raise CapabilityError("persisted growth turn differs from immutable request")
        db.execute("UPDATE growth_requests SET session_id=?,turn_id=? WHERE request_id=?",
                   (session_id, turn_id, rid))
        self._status(db, rid, "implementing", {"session_id": session_id, "turn_id": turn_id,
                     "allowed_changed_files": deliverables, "live": False})

    @staticmethod
    def _same_persisted_turn(existing: EngineeringTurn, expected: EngineeringTurn) -> bool:
        """Accept legacy absent handoff fields without rewriting an already queued turn."""
        before, after = existing.to_mapping(), expected.to_mapping()
        for field, absent in (("effect", None), ("source_request_id", None),
                              ("constraints", []), ("acceptance_criteria", [])):
            if before.get(field) == absent:
                before[field] = after[field]
        return before == after

    def advance(self, request_id: str) -> dict:
        with self._db() as db:
            request = self._get(db, request_id)
            try:
                if request["status"] == "requested":
                    self._dispatch(db, request)
                elif request["status"] == "implementing":
                    state = self.engineering_store.load(request["session_id"])
                    if state.current_turn_id != request["turn_id"]:
                        raise CapabilityError("engineering turn identity changed")
                    if state.status not in {"pending", "running", "idle"}:
                        result = self.engineering_store.load_result(state.session_id, request["turn_id"])
                        if result.status != state.status:
                            raise CapabilityError("Engineering terminal state and result disagree")
                        if result.status != "completed":
                            self._status(db, request_id, result.status, {"engineering_result": result.to_mapping(),
                                                                       "live": False})
                        else:
                            self._validate_candidate(db, request, state, result.to_mapping())
            except (ValueError, EngineeringProtocolError, OSError, SyntaxError,
                    RecursionError, subprocess.SubprocessError) as exc:
                self._status(db, request_id, "failed", {"reason": str(exc)[:1500],
                                                       "error_type": type(exc).__name__, "live": False})
            return self._get(db, request_id)

    def _snapshot(self, request: dict, state) -> tuple[dict, dict]:
        if not state.workspace_path or not state.baseline_commit or not state.workspace_branch:
            raise CapabilityError("completed Engineering result has no isolated workspace evidence")
        workspace = Path(state.workspace_path).resolve()
        if Path(state.repository).resolve() != self.repository:
            raise CapabilityError("Engineering session repository identity changed")
        if workspace == self.repository or self.repository in workspace.parents:
            raise CapabilityError("candidate must come from a separate Engineering worktree")
        common = Path(_git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        source_common = Path(_git(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if common != source_common:
            raise CapabilityError("candidate worktree is unrelated to the owned repository")
        if _git(workspace, "branch", "--show-current") != state.workspace_branch:
            raise CapabilityError("candidate branch differs from Engineering session")
        if state.workspace_branch != "hikari/engineering/" + state.session_id:
            raise CapabilityError("candidate is not on its dedicated growth branch")
        commit = _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "merge-base", "--is-ancestor", state.baseline_commit, commit)
        if _git(workspace, "status", "--porcelain"):
            raise CapabilityError("candidate worktree has uncommitted changes")
        directory = self.candidate_directory(request)
        names = ("recipe.json", "tests.json") if request["implementation_kind"] == "recipe" else (
            "manifest.json", "implementation.py", "test_implementation.py")
        allowed = {directory + "/" + name for name in names}
        changed = set(filter(None, _git(workspace, "diff", "--name-only", "-z",
                                       state.baseline_commit, commit).split("\0")))
        if not changed or changed - allowed:
            raise CapabilityError("candidate changed files outside its immutable implementation boundary")
        content, hashes = {}, {}
        for name in names:
            relative = directory + "/" + name
            path = workspace / relative
            if not path.is_file() or path.resolve() != path or path.stat().st_size > MAX_BYTES:
                raise CapabilityError("candidate file is missing, linked outside, or oversized")
            committed = subprocess.run(["git", "-C", str(workspace), "cat-file", "blob", commit + ":" + relative],
                                       capture_output=True, timeout=15)
            if committed.returncode or len(committed.stdout) > MAX_BYTES:
                raise CapabilityError("candidate file is not the committed Engineering artifact")
            # Read the committed snapshot; working-tree CRLF conversion is harmless.
            raw = committed.stdout
            content[name] = raw.decode("utf-8")
            hashes[name] = hashlib.sha256(raw).hexdigest()
        if _git(workspace, "rev-parse", "HEAD") != commit or _git(workspace, "status", "--porcelain"):
            raise CapabilityError("candidate workspace changed during validation snapshot")
        return content, {"workspace": str(workspace), "branch": state.workspace_branch,
                         "baseline_commit": state.baseline_commit, "commit": commit,
                         "files": hashes, "changed_files": sorted(changed)}

    def _validate_candidate(self, db, request: dict, state, engineering_result: dict) -> None:
        content, provenance = self._snapshot(request, state)
        evidence = {"engineering_result": engineering_result, "provenance": provenance,
                    "implementation_kind": request["implementation_kind"], "live": False}
        # Persist evidence before interpretation so later failures do not erase the failed artifact identity.
        self._event(db, request["request_id"], "candidate_observed", evidence)
        if request["implementation_kind"] == "recipe":
            recipe = self.runtime.validate(json.loads(content["recipe.json"]))
            self._match_contract(recipe, request)
            tests = json.loads(content["tests.json"])
            self._validate_cases(tests, request["input_schema"], request["output_schema"])
            results = []
            for origin, cases in (("immutable_acceptance", request["acceptance_cases"]), ("candidate_tests", tests)):
                for index, case in enumerate(cases):
                    actual = self.runtime.invoke(recipe, case["input"])
                    passed = canonical(actual) == canonical(case["expected"])
                    outcome = {"origin": origin, "index": index, "passed": passed,
                               "actual": actual, "expected": case["expected"]}
                    self._event(db, request["request_id"], "validation_case", outcome)
                    results.append(outcome)
                    if not passed:
                        raise CapabilityError(f"{origin} case {index} failed")
            evidence["validation"] = {"runner": "hikari-owned RecipeRuntime v1", "passed": True,
                                       "case_count": len(results)}
            candidate = {"kind": "recipe", "recipe": recipe, "source_files": content,
                         "provenance": provenance}
            status = "candidate_tested"
        else:
            manifest = json.loads(content["manifest.json"])
            if not isinstance(manifest, dict) or set(manifest) != {
                "format", "owner", "capability_id", "version", "input_schema", "output_schema",
                "entrypoint", "permissions",
            } or manifest["format"] != "hikari.native.v1" or manifest["owner"] != "hikari.private":
                raise CapabilityError("invalid native candidate manifest")
            self._match_contract(manifest, request)
            if manifest["entrypoint"] != "implementation.py:invoke":
                raise CapabilityError("native candidate must expose implementation.py:invoke")
            if not isinstance(manifest["permissions"], list) or not all(
                isinstance(item, str) and item.strip() for item in manifest["permissions"]
            ):
                raise CapabilityError("native permissions must be an explicit list")
            tree = ast.parse(content["implementation.py"], filename="implementation.py")
            if not any(isinstance(node, ast.FunctionDef) and node.name == "invoke" for node in tree.body):
                raise CapabilityError("native candidate lacks an invoke function")
            tests_tree = ast.parse(content["test_implementation.py"], filename="test_implementation.py")
            if not any(isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
                       for node in ast.walk(tests_tree)):
                raise CapabilityError("native candidate must include test functions")
            evidence["validation"] = {"runner": "AST syntax inspection only", "passed": None,
                "reason": "native runtime tests and deployment require an operator-reviewed execution boundary"}
            candidate = {"kind": "native", "manifest": manifest, "source_files": content,
                         "provenance": provenance}
            status = "candidate_implemented"
        digest = _digest(candidate)
        existing = db.execute("SELECT digest,request_id FROM growth_candidates WHERE capability_id=? AND version=?",
                              (request["capability_id"], request["version"])).fetchone()
        if existing and (existing["digest"] != digest or existing["request_id"] != request["request_id"]):
            raise CapabilityError("capability version is already owned; use a new explicit version")
        db.execute("INSERT OR IGNORE INTO growth_candidates VALUES(?,?,?,?,?)",
                   (request["capability_id"], request["version"], request["request_id"], digest, canonical(candidate)))
        db.execute("UPDATE growth_requests SET candidate_digest=? WHERE request_id=?",
                   (digest, request["request_id"]))
        evidence["candidate_digest"] = digest
        self._status(db, request["request_id"], status, evidence)

    @staticmethod
    def _match_contract(candidate: dict, request: dict) -> None:
        for field in ("capability_id", "version", "input_schema", "output_schema"):
            if canonical(candidate[field]) != canonical(request[field]):
                raise CapabilityError("candidate altered the immutable " + field)

    def operator_activate(self, request_id: str, *, approved_digest: str, operator_ref: str) -> dict:
        """Trusted configuration/CLI seam. Never expose as a model-requested tool."""
        if not isinstance(operator_ref, str) or not operator_ref.strip():
            raise CapabilityError("activation requires an operator decision reference")
        with self._db() as db:
            request = self._get(db, request_id)
            if request["implementation_kind"] != "recipe":
                raise CapabilityError("native candidate requires operator validation, deployment and a reviewed host adapter")
            if request["status"] not in {"candidate_tested", "active", "resumed"}:
                raise CapabilityError("only a tested candidate can be activated")
            if not approved_digest or approved_digest != request["candidate_digest"]:
                raise CapabilityError("operator approval does not match candidate content")
            candidate = self._candidate(db, request["capability_id"], request["version"], approved_digest)
            self.runtime.validate(candidate["recipe"])
            existing = db.execute("SELECT digest FROM growth_active WHERE capability_id=? AND version=?",
                                  (request["capability_id"], request["version"])).fetchone()
            if existing:
                if existing["digest"] != approved_digest:
                    raise CapabilityError("active version content differs")
                return request
            db.execute("INSERT INTO growth_active VALUES(?,?,?,?,?)",
                       (request["capability_id"], request["version"], approved_digest, operator_ref, time.time()))
            self._status(db, request_id, "active", {**request["evidence"], "live": True,
                         "operator_ref": operator_ref, "activation_kind": "pure_recipe_in_owned_runtime"})
            return self._get(db, request_id)

    @staticmethod
    def _candidate(db, capability_id: str, version: int, digest: str) -> dict:
        row = db.execute("SELECT * FROM growth_candidates WHERE capability_id=? AND version=?",
                         (capability_id, version)).fetchone()
        if row is None or row["digest"] != digest:
            raise CapabilityError("candidate registry evidence is missing or changed")
        candidate = json.loads(row["candidate_json"])
        if _digest(candidate) != digest:
            raise CapabilityError("candidate snapshot content no longer matches evidence")
        return candidate

    def _invoke(self, db, capability_id: str, inputs: object, *, turn: UserTurn, version: int) -> object:
        _private(turn)
        capability_identity(capability_id, version)
        row = db.execute("SELECT a.digest,c.request_id FROM growth_active a JOIN growth_candidates c "
                         "ON a.capability_id=c.capability_id AND a.version=c.version "
                         "WHERE a.capability_id=? AND a.version=?", (capability_id, version)).fetchone()
        if row is None:
            raise CapabilityError("capability version is not active")
        request = self._get(db, row["request_id"])
        _same_owner(request["source_turn"], turn)
        candidate = self._candidate(db, capability_id, version, row["digest"])
        if candidate["kind"] != "recipe":
            raise CapabilityError("native candidates cannot run in Resident")
        return self.runtime.invoke(candidate["recipe"], inputs)

    def invoke(self, capability_id: str, inputs: object, *, turn: UserTurn, version: int = 1) -> object:
        with self._db() as db:
            return self._invoke(db, capability_id, inputs, turn=turn, version=version)

    def resume(self, request_id: str, *, turn: UserTurn) -> dict:
        with self._db() as db:
            request = self._get(db, request_id)
            _same_owner(request["source_turn"], turn)
            if request["status"] == "resumed":
                return request
            if request["status"] != "active" or request["resume_input"] is None:
                raise CapabilityError("resume requires an active capability and saved original inputs")
            result = self._invoke(db, request["capability_id"], request["resume_input"],
                                  turn=turn, version=request["version"])
            db.execute("UPDATE growth_requests SET result_json=? WHERE request_id=?", (canonical(result), request_id))
            self._status(db, request_id, "resumed", {**request["evidence"],
                         "resumed_source_ref": request["source_ref"], "result_digest": _digest(result)})
            return self._get(db, request_id)

    def retry(self, request_id: str, *, turn: UserTurn) -> dict:
        with self._db() as db:
            request = self._get(db, request_id)
            _same_owner(request["source_turn"], turn)
            if request["status"] not in {"failed", "blocked"}:
                raise CapabilityError("only failed or blocked implementation requests can be retried")
            db.execute("UPDATE growth_requests SET attempt=attempt+1,session_id=NULL,turn_id=NULL "
                       "WHERE request_id=?", (request_id,))
            self._status(db, request_id, "requested", {"retry_of_attempt": request["attempt"],
                                                       "previous_evidence_preserved": True})
            return self._get(db, request_id)

    def get(self, request_id: str) -> dict:
        with self._db() as db:
            return self._get(db, request_id)

    def list_requests(self) -> list[dict]:
        with self._db() as db:
            ids = db.execute("SELECT request_id FROM growth_requests ORDER BY created_at,request_id").fetchall()
            return [self._get(db, row[0]) for row in ids]

    def events(self, request_id: str) -> list[dict]:
        with self._db() as db:
            self._get(db, request_id)
            rows = db.execute("SELECT * FROM growth_events WHERE request_id=? ORDER BY sequence",
                              (request_id,)).fetchall()
            return [{"sequence": row["sequence"], "kind": row["kind"],
                     "evidence": json.loads(row["evidence_json"]), "created_at": row["created_at"]} for row in rows]

    def advance_all(self) -> list[dict]:
        return [self.advance(item["request_id"]) for item in self.list_requests()
                if item["status"] in {"requested", "implementing"}]

    def describe(self, *, turn: UserTurn | None = None) -> dict:
        """Scope model context to its exact private owner; no-turn is operator-only."""
        if turn is not None:
            _private(turn)
        requests = []
        for item in self.list_requests():
            if turn is not None:
                try:
                    _same_owner(item["source_turn"], turn)
                except CapabilityError:
                    continue
            requests.append(item)
        active_interfaces = []
        with self._db() as db:
            for item in requests:
                active = db.execute("SELECT a.digest FROM growth_active a JOIN growth_candidates c "
                                    "ON a.capability_id=c.capability_id AND a.version=c.version "
                                    "WHERE a.capability_id=? AND a.version=? AND c.request_id=?",
                                    (item["capability_id"], item["version"], item["request_id"])).fetchone()
                if active is None:
                    continue
                candidate = self._candidate(db, item["capability_id"], item["version"], active["digest"])
                if candidate["kind"] != "recipe":
                    continue
                recipe = self.runtime.validate(candidate["recipe"])
                active_interfaces.append({"capability_id": item["capability_id"], "version": item["version"],
                    "input_schema": recipe["input_schema"], "output_schema": recipe["output_schema"],
                    "permissions": [], "candidate_digest": active["digest"]})
        return {"version": 1, "implementation_enabled": self.implementation_enabled,
                "scope": "private", "recipe_domain": "pure bounded text and line-list transformations",
                "services": SERVICE_CONTRACTS,
                "unsupported_recipe_effects": ["network", "filesystem", "shell", "external messaging",
                                               "credentials", "permission expansion"],
                "native_candidates": "implemented source only; operator validation/deployment/host adapter required",
                "activation": "explicit operator-approved candidate digest; never model-declared availability",
                "active_interfaces": active_interfaces,
                "requests": [{key: item[key] for key in ("request_id", "status", "capability_id", "version",
                    "implementation_kind", "candidate_digest", "evidence")} for item in requests]}
