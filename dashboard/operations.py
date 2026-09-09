from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from core.delegation import hikari_engineering_capabilities
from engineering.goal import EngineeringGoalStore, EngineeringGoalStoreError
from engineering.heartbeat import EngineeringWorkerHeartbeatStore, _process_alive
from engineering.session import EngineeringSessionStore, EngineeringProtocolError
from resident.telemetry import read_observation

from .settings import DashboardSettings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_rows(path: Path, sql: str, parameters: tuple = ()) -> list[dict]:
    """Observability must never initialize, migrate, recover or mutate live stores."""
    if not path.is_file():
        return []
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]
    finally:
        connection.close()


class DashboardOperations:
    def __init__(self, state_dir: Path, settings: DashboardSettings):
        self.root = Path(state_dir)
        self.settings = settings

    def snapshot(self) -> dict:
        errors: list[dict] = []
        sessions = []
        store = EngineeringSessionStore(self.root / "engineering")
        if store.root.is_dir():
            for directory in store.root.iterdir():
                if not directory.is_dir():
                    continue
                try:
                    state = store.load(directory.name)
                    current = None
                    result = None
                    if state.current_turn_id:
                        current = store.load_turn(state.session_id, state.current_turn_id)
                        if state.status in {"completed", "failed", "blocked"}:
                            result = store.load_result(state.session_id, state.current_turn_id)
                    sessions.append({
                        "session_id": state.session_id, "project_id": state.project_id,
                        "status": state.status, "current_turn_id": state.current_turn_id,
                        "goal": current.intent if current else "尚未入队",
                        "summary": result.message if result else state.latest_summary,
                        "updated_at": state.updated_at, "branch": state.workspace_branch,
                        "changed_files": list(result.changed_files) if result else [],
                        "result_verified": result is not None,
                    })
                except (EngineeringProtocolError, ValueError, TypeError, OSError) as exc:
                    errors.append({"source": "session", "id": directory.name, "error": type(exc).__name__})
        try:
            goals = [goal.to_mapping() for goal in EngineeringGoalStore(self.root / "engineering_goals").list_states()]
        except EngineeringGoalStoreError as exc:
            goals = []
            errors.append({"source": "goals", "error": str(exc)})

        tables = {
            "user_model_jobs": ("user_model_jobs.db", "SELECT source_ref,status,attempts,retry_at,last_error_type,created_at,updated_at FROM user_model_jobs ORDER BY sequence DESC LIMIT 60"),
            "tasks": ("conversation_tasks.db", "SELECT source_ref,turn_json,intent_json,status,evidence_json,created_at,updated_at FROM task_requests ORDER BY created_at DESC LIMIT 60"),
            "receipts": ("conversation_receipts.db", "SELECT request_id, channel, conversation_id, user_text, reply_text, created_at FROM conversation_receipts ORDER BY rowid DESC LIMIT 60"),
            "claims": ("conversation_receipts.db", "SELECT request_id, channel, conversation_id, state, created_at FROM conversation_request_claims ORDER BY rowid DESC LIMIT 60"),
            "deliveries": ("proactive_delivery.db", "SELECT delivery_id, channel, state, attempts, last_error, updated_at FROM proactive_delivery_outbox ORDER BY rowid DESC LIMIT 60"),
        }
        records = {}
        for name, (filename, query) in tables.items():
            try:
                records[name] = read_rows(self.root / filename, query)
            except sqlite3.Error as exc:
                records[name] = []
                # Claims are additive; absence on an older runtime isn't corruption.
                if not (name == "claims" and "no such table" in str(exc)):
                    errors.append({"source": name, "error": type(exc).__name__})
        for task in records["tasks"]:
            for key in ("turn", "intent", "evidence"):
                try:
                    task[key] = json.loads(task.pop(key + "_json"))
                except (ValueError, KeyError):
                    task[key] = {}
                    errors.append({"source": "tasks", "id": task["source_ref"], "error": "invalid task evidence"})

        worker = {"id": "worker", "label": "Engineering Worker", "status": "unknown",
                  "message": "没有可读取的心跳", "observed_at": _now()}
        try:
            heartbeat = EngineeringWorkerHeartbeatStore(self.root / "engineering_worker.json").load()
            if heartbeat:
                age = max(0, time.time() - heartbeat.updated_at)
                alive = _process_alive(heartbeat.pid)
                worker.update(status="healthy" if alive and age <= 10 else "offline",
                              pid=heartbeat.pid, age_seconds=round(age, 1),
                              message="进程存活且心跳新鲜" if alive and age <= 10 else "进程已退出或心跳过期")
        except (OSError, ValueError):
            worker.update(status="error", message="心跳记录不可读取")
        values = self.settings.values()
        observations = {}
        for component in ("conversation", "model", "qq"):
            entry = {"id": component, "label": {"conversation": "Conversation", "model": "对话模型", "qq": "OneBot 链路"}[component],
                     "status": "unknown", "message": "尚无运行进程提供的观察证据"}
            try:
                observation = read_observation(self.root, component)
                if observation:
                    age = max(0, time.time() - observation["observed_at"])
                    alive = _process_alive(observation["pid"])
                    maximum = 300 if component == "model" else 30
                    if component == "qq":
                        maximum = min(600, max(15, float(observation.get("details", {}).get("observation_ttl_seconds", 30))))
                    entry.update(observation.get("details", {}), observed_at=observation["observed_at"],
                                 status=observation["status"] if alive and age < maximum else "unknown",
                                 age_seconds=round(age, 1))
                    entry["message"] = ("最近真实模型调用的结果" if component == "model" else "由运行组件报告的连接证据") if alive and age < maximum else "观察已过期或原进程已退出"
            except (OSError, ValueError, KeyError):
                entry.update(status="error", message="运行观察记录不可读取")
            observations[component] = entry
        activation_known = observations["conversation"]["status"] == "healthy" and isinstance(observations["conversation"].get("engineering_enabled"), bool)
        enabled = observations["conversation"].get("engineering_enabled") is True
        capabilities = [
            {"key": key, **capability.to_mapping(),
             "delegated": (capability.delegated and enabled) if activation_known else (False if not capability.delegated else None),
             "runtime_ready": (worker["status"] == "healthy" and enabled) if activation_known and capability.available else (False if not capability.available else None)}
            for key, capability in hikari_engineering_capabilities(True).items()
        ]
        observations["model"]["configured_model"] = values.get("HIKARI_MODEL_NAME", "")
        return {"generated_at": _now(), "errors": errors, "complete": not errors,
                "goals": sorted(goals, key=lambda x: x["updated_at"], reverse=True)[:60],
                "sessions": sorted(sessions, key=lambda x: x["updated_at"], reverse=True)[:60],
                "worker": worker, "capabilities": capabilities,
                "capability_basis": "runtime_report_and_worker_heartbeat", **observations, **records}
