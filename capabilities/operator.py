"""Operator-owned pure capability activation; independent of dashboard transport."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from engineering.session import EngineeringSessionStore
from resident.file_locks import FileUpdateBusy, serialized_file_update

from .growth import CapabilityGrowth
from .runtime import CapabilityError, RecipeRuntime, SERVICE_CONTRACTS, canonical


class OperatorPolicyConflict(ValueError):
    """A concurrent operator changed or is saving this policy."""


def assert_external_state(path: Path, repository: Path) -> None:
    """Authority truth must remain outside every candidate/source Git worktree."""
    resolved = Path(path).resolve()
    repository = Path(repository).resolve()
    if resolved == repository or repository in resolved.parents:
        raise ValueError("操作人授权配置和能力注册表必须位于源码工作区之外")
    for directory in resolved.parents:
        if (directory / ".git").exists():
            raise ValueError("操作人授权配置和能力注册表不能放在任何 Git 工作区内")


class CapabilityOperatorControls:
    """Reusable trusted operator/continuation seam with read-only observations.

    A transport must enforce operator authentication and request-origin checks.
    Saving a policy does not activate candidates; the trusted task continuation
    applies explicit policy through auto_activate_tested_capabilities.
    """

    def __init__(self, repository: Path, state_dir: Path):
        self.repository = Path(repository).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.growth_policy_path = self.state_dir / "capability_growth_policy.json"
        self.growth_path = self.state_dir / "capability_growth.db"

    def _assert_external_state(self, path: Path) -> None:
        assert_external_state(path, self.repository)

    @staticmethod
    def _validate_growth_policy(document: object) -> dict:
        if not isinstance(document, dict) or set(document) != {
            "version", "auto_activate_pure_recipes", "allowed_services",
        }:
            raise ValueError("能力授权配置必须且只能包含版本、自动启用开关和允许的纯服务")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("能力授权配置版本不受支持")
        if type(document["auto_activate_pure_recipes"]) is not bool:
            raise ValueError("纯能力自动启用开关必须为布尔值")
        services = document["allowed_services"]
        if not isinstance(services, list) or not all(
            isinstance(item, str) and item in SERVICE_CONTRACTS for item in services
        ) or len(set(services)) != len(services):
            raise ValueError("允许的服务必须是不重复的已实现纯服务名称")
        if document["auto_activate_pure_recipes"] and not services:
            raise ValueError("开启纯能力自动启用前必须明确允许的服务")
        return json.loads(canonical(document))

    def get_growth_policy(self) -> dict:
        try:
            with self.growth_policy_path.open("rb") as stream:
                raw = stream.read(65_537)
        except FileNotFoundError:
            return {"revision": "absent", "configured": False,
                    "document": {"version": 1, "auto_activate_pure_recipes": False, "allowed_services": []},
                    "available_services": sorted(SERVICE_CONTRACTS)}
        if len(raw) > 65_536:
            raise ValueError("能力授权配置过大")
        document = self._validate_growth_policy(json.loads(raw.decode("utf-8")))
        return {"revision": hashlib.sha256(raw).hexdigest(), "configured": True,
                "document": document, "available_services": sorted(SERVICE_CONTRACTS)}

    @contextmanager
    def _growth_policy_lock(self):
        self._assert_external_state(self.growth_policy_path)
        try:
            with serialized_file_update(self.growth_policy_path):
                yield
        except FileUpdateBusy:
            raise OperatorPolicyConflict("能力授权配置正在使用，请重新读取后重试") from None

    def save_growth_policy(self, document: dict, revision: str) -> dict:
        document = self._validate_growth_policy(document)
        if not isinstance(revision, str) or not revision:
            raise ValueError("保存能力授权配置需要读取时的版本")
        with self._growth_policy_lock():
            if self.get_growth_policy()["revision"] != revision:
                raise OperatorPolicyConflict("能力授权配置已被修改，请重新读取后再保存")
            temporary = self.growth_policy_path.with_name(self.growth_policy_path.name + "." + uuid4().hex + ".tmp")
            try:
                raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                with temporary.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.growth_policy_path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.get_growth_policy()

    @contextmanager
    def _growth_read(self):
        if not self.growth_path.is_file():
            raise CapabilityError("没有现有的能力注册表")
        connection = sqlite3.connect(self.growth_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _tested_recipe(db, request_id: str, digest: str) -> tuple[dict, dict]:
        request = CapabilityGrowth._get(db, request_id)
        if request["implementation_kind"] != "recipe":
            raise CapabilityError("原生能力仍需操作人审核验证、部署并接入受限宿主，不能直接启用")
        if request["status"] not in {"candidate_tested", "active", "resumed"}:
            raise CapabilityError("只能启用经过独立验证的候选能力")
        if not digest or request["candidate_digest"] != digest:
            raise CapabilityError("启用请求的摘要与当前候选实现不一致")
        validation = request["evidence"].get("validation", {})
        if (validation.get("passed") is not True or validation.get("runner") != "hikari-owned RecipeRuntime v1"
                or type(validation.get("case_count")) is not int or validation["case_count"] < 1):
            raise CapabilityError("候选能力缺少宿主独立执行的测试证据")
        candidate = CapabilityGrowth._candidate(db, request["capability_id"], request["version"], digest)
        if candidate["kind"] != "recipe":
            raise CapabilityError("候选类型不是纯配方能力")
        RecipeRuntime.validate(candidate["recipe"])
        return request, candidate

    def growth_snapshot(self) -> dict:
        result = {"configured": self.growth_path.is_file(), "status": "absent", "requests": [],
                  "active_interfaces": [], "errors": []}
        if not result["configured"]:
            return result
        try:
            with self._growth_read() as db:
                rows = db.execute("SELECT request_id FROM growth_requests ORDER BY updated_at DESC,request_id LIMIT 100").fetchall()
                for row in rows:
                    try:
                        request = CapabilityGrowth._get(db, row["request_id"])
                        request["activatable"] = False
                        if request["status"] == "candidate_tested" and request["implementation_kind"] == "recipe":
                            self._tested_recipe(db, request["request_id"], request["candidate_digest"])
                            request["activatable"] = True
                        result["requests"].append(request)
                    except (ValueError, KeyError, TypeError) as exc:
                        result["errors"].append({"source": "request", "request_id": row["request_id"],
                                                 "error": str(exc)[:1000]})
                rows = db.execute("SELECT a.*,c.request_id FROM growth_active a JOIN growth_candidates c "
                                  "ON a.capability_id=c.capability_id AND a.version=c.version").fetchall()
                for row in rows:
                    try:
                        request, candidate = self._tested_recipe(db, row["request_id"], row["digest"])
                        recipe = candidate["recipe"]
                        result["active_interfaces"].append({
                            "request_id": request["request_id"], "capability_id": request["capability_id"],
                            "version": request["version"], "candidate_digest": row["digest"],
                            "input_schema": recipe["input_schema"], "output_schema": recipe["output_schema"],
                            "operator_ref": row["operator_ref"], "activated_at": row["activated_at"],
                            "permissions": [],
                        })
                    except (ValueError, KeyError, TypeError) as exc:
                        result["errors"].append({"source": "active_candidate", "request_id": row["request_id"],
                                                 "error": str(exc)[:1000]})
                result["status"] = "error" if result["errors"] else "ready"
        except (sqlite3.Error, OSError, ValueError) as exc:
            result["status"] = "error"
            result["errors"].append({"source": "growth_registry", "error": str(exc)[:1000]})
        return result

    def _growth_writer(self) -> CapabilityGrowth:
        self._assert_external_state(self.growth_path)
        if not self.growth_path.is_file():
            raise CapabilityError("没有现有的能力注册表")
        return CapabilityGrowth(self.growth_path,
            engineering_store=EngineeringSessionStore(self.state_dir / "engineering"),
            repository=self.repository, implementation_enabled=False)

    def operator_activate_capability(self, request_id: str, digest: str) -> dict:
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("能力请求标识不正确")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("候选实现摘要不正确")
        with self._growth_read() as db:
            self._tested_recipe(db, request_id, digest)
        return self._growth_writer().operator_activate(request_id, approved_digest=digest,
                                                      operator_ref="dashboard:manual:" + uuid4().hex)

    def auto_activate_tested_capabilities(self) -> dict:
        """Trusted continuation only: apply current explicit policy without expanding it."""
        snapshot = self.get_growth_policy()
        result = {"policy_revision": snapshot["revision"],
                  "enabled": snapshot["document"]["auto_activate_pure_recipes"],
                  "activated": [], "skipped": [], "errors": []}
        if not result["enabled"] or not self.growth_path.is_file():
            return result
        with self._growth_policy_lock():
            policy = self.get_growth_policy()
            result.update(policy_revision=policy["revision"], enabled=policy["document"]["auto_activate_pure_recipes"])
            if not result["enabled"]:
                return result
            allowed = set(policy["document"]["allowed_services"])
            with self._growth_read() as db:
                ids = [row[0] for row in db.execute("SELECT request_id FROM growth_requests "
                       "WHERE status IN ('candidate_tested','candidate_implemented') ORDER BY updated_at,request_id")]
            for request_id in ids:
                try:
                    with self._growth_read() as db:
                        request = CapabilityGrowth._get(db, request_id)
                        if request["implementation_kind"] != "recipe":
                            result["skipped"].append({"request_id": request_id, "reason": "native_requires_operator_deployment"})
                            continue
                        request, candidate = self._tested_recipe(db, request_id, request["candidate_digest"])
                    recipe = candidate["recipe"]
                    used = {step["service"] for step in recipe["steps"]}
                    if not used <= allowed:
                        result["skipped"].append({"request_id": request_id,
                            "reason": "services_outside_operator_policy", "services": sorted(used - allowed)})
                        continue
                    cases = json.loads(candidate["source_files"]["tests.json"])
                    CapabilityGrowth._validate_cases(cases, request["input_schema"], request["output_schema"])
                    runtime = RecipeRuntime()
                    for case in [*request["acceptance_cases"], *cases]:
                        if canonical(runtime.invoke(recipe, case["input"])) != canonical(case["expected"]):
                            raise CapabilityError("候选能力未通过自动启用前的宿主独立复验")
                    activated = self._growth_writer().operator_activate(request_id,
                        approved_digest=request["candidate_digest"],
                        operator_ref="dashboard:pure-policy:" + policy["revision"] + ":" + uuid4().hex)
                    result["activated"].append(activated)
                except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
                    result["errors"].append({"request_id": request_id, "error": str(exc)[:1000]})
        return result
