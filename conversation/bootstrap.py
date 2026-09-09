"""One production assembly point for private action services and natural reply checks."""
from pathlib import Path

from capabilities import CapabilityGrowth
from capabilities.operator import CapabilityOperatorControls
from engineering.session import EngineeringSessionStore
from integrations.github.actions import GitHubActionService

from .claim_guard import ActionClaimGuard
from .engine import ConversationEngine
from .task_router import ConversationTaskRouter
from .task_store import ConversationTaskStore
from .github_workflow import GitHubConversationWorkflow
from user_model.jobs import UserModelJobStore, UserModelJobWorker


def build_private_task_router(engine, *, repository: Path, state_dir: Path, values: dict,
                              engineering_bridge=None):
    configured = values.get("HIKARI_GITHUB_ALLOWED_REPOSITORIES", "").strip()
    default = values.get("HIKARI_GITHUB_REPOSITORY", "").strip()
    repositories = [name.strip() for name in configured.split(",") if name.strip()] if configured else ([default] if default else None)
    github = GitHubActionService(repository, state_dir=state_dir, repositories=repositories, environment=dict(values))
    growth = CapabilityGrowth(state_dir / "capability_growth.db",
                              engineering_store=EngineeringSessionStore(state_dir / "engineering"),
                              repository=repository, implementation_enabled=engineering_bridge is not None)
    router = ConversationTaskRouter(engineering_bridge=engineering_bridge,
                                   tasks=ConversationTaskStore(state_dir / "conversation_tasks.db"),
                                   github_service=github, growth=growth)
    router.growth_operator = CapabilityOperatorControls(repository, state_dir)
    router.github_workflow = GitHubConversationWorkflow(engine.provider, github, state_dir / "github_workflows.db")
    if isinstance(engine, ConversationEngine):
        engine.response_guard = ActionClaimGuard(engine.provider, router.reply_evidence)
        if engine.user_fact_extractor is not None and engine.user_model_service is not None:
            jobs = UserModelJobStore(state_dir / "user_model_jobs.db")
            engine.assimilation_sink = jobs
            router.user_model_worker = UserModelJobWorker(jobs, engine.user_fact_extractor, engine.user_model_service)
    return router
