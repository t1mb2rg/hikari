from attention import AttentionPolicy
from events import Event


def test_attention_uses_event_type_policy():
    policy = AttentionPolicy(
        threshold=0.7,
        event_importance={"test.important": 0.85},
    )

    decision = policy.evaluate(
        Event(
            event_type="test.important",
            source="test",
            content="Something changed",
        )
    )

    assert decision.should_intervene is True
    assert decision.importance == 0.85
    assert decision.reason == "event type policy"


def test_sensor_hint_can_override_default_policy():
    policy = AttentionPolicy(
        threshold=0.7,
        event_importance={"test.event": 0.2},
    )

    decision = policy.evaluate(
        Event(
            event_type="test.event",
            source="test",
            content="Something changed",
            context={"importance_hint": 0.95},
        )
    )

    assert decision.should_intervene is True
    assert decision.importance == 0.95
    assert decision.reason == "sensor importance hint"


def test_attention_clamps_scores_and_stays_silent_below_threshold():
    policy = AttentionPolicy(
        threshold=0.7,
        default_importance=-10,
    )

    decision = policy.evaluate(
        Event(
            event_type="test.quiet",
            source="test",
            content="Background noise",
        )
    )

    assert decision.should_intervene is False
    assert decision.importance == 0.0
