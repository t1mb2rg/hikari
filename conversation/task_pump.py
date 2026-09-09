"""Project task/growth progress into truthful status and deduplicated delivery."""
from __future__ import annotations

from dataclasses import asdict
import sqlite3

from core.delivery import DeliveryOutbox, DeliveryRequest
from .models import UserTurn


class ConversationTaskPump:
    def __init__(self, router, outbox: DeliveryOutbox):
        self.router, self.outbox = router, outbox

    def github_once(self):
        workflow = getattr(self.router, "github_workflow", None)
        for task in self.router.tasks.unfinished():
            if task["intent"]["kind"] != "github" or task["status"] not in {"planned", "pending", "repair_needed", "repair_pending", "repair_running"}:
                continue
            intent = task["intent"]
            turn = UserTurn(**task["turn"])
            if turn.is_shared:
                continue
            if task["status"] == "planned":
                # A task row alone does not prove the GitHub workflow accepted
                # intake. Resume only a pre-existing exact frozen workflow request;
                # run() retains ownership of uncertain write detection and replay.
                try:
                    saved = workflow.existing_request(task["source_ref"]) if workflow is not None else None
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    saved = None
                if (saved is None or saved.get("turn") != asdict(turn)
                        or saved.get("goal") != (intent.get("goal") or turn.text)
                        or saved.get("constraints") != intent.get("constraints", [])
                        or saved.get("acceptance_criteria") != intent.get("acceptance_criteria", [])
                        or saved.get("allow_local_repair", False) != intent.get("allow_local_repair", False)
                        or saved.get("initial_action") != (intent.get("action") or None)
                        or saved.get("initial_arguments") != (intent.get("arguments") or {})):
                    self.router.tasks.update_evidence(task["source_ref"], status="unknown", evidence={
                        "status": "unknown", "source_ref": task["source_ref"],
                        "error": "没有与原始私人请求匹配的 GitHub 工作流记录，未重复规划或执行。",
                    })
                    return
            if task["status"] in {"repair_needed", "repair_pending", "repair_running"} or task["evidence"].get("local_repair"):
                result = task["evidence"]
            elif workflow is not None:
                result = workflow.run({"goal": intent["goal"] or turn.text, "constraints": intent["constraints"],
                                       "acceptance_criteria": intent["acceptance_criteria"],
                                       "allow_local_repair": intent.get("allow_local_repair", False),
                                       "requested_actions": [intent["action"]] if intent["action"] else []},
                                      source_ref=task["source_ref"], turn=turn, initial_action=intent["action"] or None,
                                      initial_arguments=intent["arguments"] or {})
            else:
                continue
            result = self.router._github_repair_result(getattr(self.router, "engine", None), task["source_ref"], turn, intent, result)
            child = (result.get("local_repair") or {}).get("engineering") or {}
            has_child = bool(child.get("goal_id") or child.get("turn_id"))
            if not has_child and result["status"] not in {"pending", "repair_needed", "repair_pending", "repair_running"} and turn.channel == "qq" and turn.conversation_id.startswith("private:"):
                delivery_id = f"github-workflow:{task['source_ref']}"
                if self.outbox.get(delivery_id) is None:
                    self.outbox.enqueue(DeliveryRequest(delivery_id, "qq", turn.conversation_id.removeprefix("private:"),
                                                       self.router._result_reply(turn, result).text, source="github_workflow"))
            self.router.tasks.update_evidence(task["source_ref"], status=result["status"], evidence=result)
            return

    def __call__(self):
        if self.router.growth:
            self.router.growth.advance_all()
            operator = getattr(self.router, "growth_operator", None)
            if operator is not None:
                try:
                    report = operator.auto_activate_tested_capabilities()
                except Exception as exc:
                    report = {"errors": [{"error": type(exc).__name__}]}
                if report.get("errors"):
                    import logging
                    logging.getLogger(__name__).warning("能力自动启用有 %d 个待处理错误", len(report["errors"]))
        for task in self.router.tasks.unfinished():
            kind = task["intent"]["kind"]
            turn = UserTurn(**task["turn"])
            if turn.is_shared:
                continue
            source = task["source_ref"]
            if kind == "engineering" and self.router.engineering:
                facts = self.router._engineering_evidence(source, turn)
                if facts.get("status") != task["status"]:
                    self.router.tasks.update_evidence(source, status=facts["status"], evidence=facts)
                # Existing Engineering delivery retains ownership of those results.
            elif kind == "capability" and self.router.growth:
                request_id = task["evidence"].get("request_id")
                if not request_id:
                    # Growth intake may have committed before the parent receipt.
                    # Recover only the exact immutable source; never rerun a model
                    # or reconstruct an uncertain request from a vague task title.
                    matches = [request for request in self.router.growth.list_requests()
                               if request.get("source_ref") == source and request.get("source_turn") == asdict(turn)]
                    if len(matches) != 1:
                        self.router.tasks.update_evidence(source, status="unknown", evidence={
                            **task["evidence"], "status": "unknown", "source_ref": source,
                            "error": "能力请求没有唯一匹配的持久来源记录，尚不能确认接单结果。",
                        })
                        continue
                    request_id = matches[0]["request_id"]
                    # Retain the nonterminal parent status until outbox projection
                    # succeeds, even when the recovered growth request is terminal.
                    self.router.tasks.update_evidence(source, status=task["status"], evidence=matches[0])
                request = self.router.growth.get(request_id)
                if request.get("source_ref") != source or request.get("source_turn") != asdict(turn):
                    self.router.tasks.update_evidence(source, status="unknown", evidence={
                        "status": "unknown", "source_ref": source, "error": "能力记录与原始私人请求身份不匹配。",
                    })
                    continue
                if request["status"] == "active" and request.get("resume_input") is not None:
                    request = self.router.growth.resume(request_id, turn=turn)
                learned = request["status"] == "active" and request.get("resume_input") is None
                if learned or request["status"] in {"candidate_tested", "candidate_implemented", "resumed", "failed", "blocked"}:
                    delivery_id = f"capability:{request_id}:{request['status']}"
                    if self.outbox.get(delivery_id) is None and turn.channel == "qq" and turn.conversation_id.startswith("private:"):
                        text = self.router._result_reply(turn, request).text
                        self.outbox.enqueue(DeliveryRequest(delivery_id=delivery_id, channel="qq",
                            recipient=turn.conversation_id.removeprefix("private:"), text=text, source="capability_growth"))
                # Outbox first: once status becomes terminal the task no longer
                # participates in unfinished(). A failed enqueue must remain retryable.
                task_status = "completed" if learned else request["status"]
                if task_status != task["status"]:
                    self.router.tasks.update_evidence(source, status=task_status, evidence=request)
