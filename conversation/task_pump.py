"""Project task/growth progress into truthful status and deduplicated delivery."""
from __future__ import annotations

from core.delivery import DeliveryOutbox, DeliveryRequest
from .models import UserTurn


class ConversationTaskPump:
    def __init__(self, router, outbox: DeliveryOutbox):
        self.router, self.outbox = router, outbox

    def github_once(self):
        workflow = getattr(self.router, "github_workflow", None)
        if workflow is None:
            return
        for task in self.router.tasks.unfinished():
            if task["intent"]["kind"] != "github" or task["status"] != "pending":
                continue
            intent = task["intent"]
            turn = UserTurn(**task["turn"])
            if turn.is_shared:
                continue
            result = workflow.run({"goal": intent["goal"] or turn.text, "constraints": intent["constraints"],
                                   "acceptance_criteria": intent["acceptance_criteria"],
                                   "requested_actions": [intent["action"]] if intent["action"] else []},
                                  source_ref=task["source_ref"], turn=turn, initial_action=intent["action"] or None,
                                  initial_arguments=intent["arguments"] or {})
            if result["status"] != "pending" and turn.channel == "qq" and turn.conversation_id.startswith("private:"):
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
                    continue
                request = self.router.growth.get(request_id)
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
