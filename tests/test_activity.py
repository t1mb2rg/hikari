from awareness import InputActivityContextProvider


def test_input_activity_provider_marks_recent_input():
    provider = InputActivityContextProvider(
        recent_input_threshold_seconds=120,
        idle_seconds_reader=lambda: 5.0,
    )

    context = provider.capture()

    assert context == {
        "supported": True,
        "recent_input": True,
        "idle_seconds": 5.0,
        "recent_input_threshold_seconds": 120.0,
    }


def test_input_activity_provider_marks_no_recent_input():
    provider = InputActivityContextProvider(
        recent_input_threshold_seconds=120,
        idle_seconds_reader=lambda: 300.0,
    )

    context = provider.capture()

    assert context["supported"] is True
    assert context["recent_input"] is False
    assert context["idle_seconds"] == 300.0


def test_input_activity_provider_degrades_to_unknown_when_unsupported():
    provider = InputActivityContextProvider(
        idle_seconds_reader=lambda: None,
    )

    assert provider.capture() == {
        "supported": False,
        "recent_input": None,
    }


def test_input_activity_provider_rejects_negative_threshold():
    try:
        InputActivityContextProvider(recent_input_threshold_seconds=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("Negative input threshold should be rejected")
