from datetime import datetime, timezone

from awareness import ContextSnapshot
from awareness.user_state import UserStateInferer


NOW = datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc)


def snapshot(providers):
    return ContextSnapshot(captured_at=NOW, providers=providers)


def test_recent_input_and_foreground_produce_interactive_state():
    state = UserStateInferer().infer(
        snapshot(
            {
                "input_activity": {"supported": True, "recent_input": True},
                "foreground": {
                    "supported": True,
                    "available": True,
                    "title": "ChatGPT",
                },
                "time": {"hour": 21},
            }
        )
    )

    assert state.engagement == "interactive"
    assert state.interruptibility == "likely_available"
    assert state.confidence == 0.8


def test_long_idle_never_claims_user_is_away():
    state = UserStateInferer().infer(
        snapshot(
            {
                "input_activity": {
                    "supported": True,
                    "recent_input": False,
                    "idle_seconds": 600.0,
                },
                "foreground": {
                    "supported": True,
                    "available": True,
                    "title": "Video",
                },
            }
        )
    )

    assert state.engagement == "passive_or_unknown"
    assert state.interruptibility == "unknown"
    assert "away" not in state.as_dict().values()


def test_missing_signals_degrade_to_unknown():
    state = UserStateInferer().infer(snapshot({}))

    assert state.engagement == "unknown"
    assert state.interruptibility == "unknown"
    assert state.confidence == 0.1


def test_current_schedule_keeps_interruptibility_unknown():
    state = UserStateInferer().infer(
        snapshot(
            {
                "input_activity": {"supported": True, "recent_input": True},
                "foreground": {
                    "supported": True,
                    "available": True,
                    "title": "Code",
                },
                "schedule": {"current": [{"title": "Meeting"}]},
            }
        )
    )

    assert state.engagement == "interactive"
    assert state.interruptibility == "unknown"
