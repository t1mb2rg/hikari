from __future__ import annotations

import logging
from pathlib import Path

from brain.model_reasoner import ChatProvider

from .extractor import ModelUserFactExtractor
from .service import UserModelService
from .store import UserModelStore


logger = logging.getLogger(__name__)


def build_user_model_runtime(
    provider: ChatProvider,
    path: str | Path,
) -> tuple[UserModelService | None, ModelUserFactExtractor | None]:
    """Open M7 state without making User Model availability a Resident gate."""

    try:
        service = UserModelService(UserModelStore(path))
        return service, ModelUserFactExtractor(provider)
    except Exception as exc:
        logger.warning(
            "Hikari continuing without persistent User Model: %s",
            type(exc).__name__,
        )
        return None, None
