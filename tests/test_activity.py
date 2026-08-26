from awareness import DeviceActivityContextProvider


def test_device_activity_provider_classifies_recent_input_as_active():
    provider = DeviceActivityContextProvider(
        active_threshold_seconds=120,
        idle_seconds_reader=lambda: 5.0,
    )

    context = provider.capture()

    assert context == {
        "supported": True,
        "state": "active",
        "idle_seconds": 5.0,
        "active_threshold_seconds": 120.0,
    }


def test_device_activity_provider_classifies_long_idle_as_idle():
    provider = DeviceActivityContextProvider(
        active_threshold_seconds=120,
        idle_seconds_reader=lambda: 300.0,
    )

    context = provider.capture()

    assert context["supported"] is True
    assert context["state"] == "idle"
    assert context["idle_seconds"] == 300.0


def test_device_activity_provider_degrades_to_unknown_when_unsupported():
    provider = DeviceActivityContextProvider(
        idle_seconds_reader=lambda: None,
    )

    assert provider.capture() == {
        "supported": False,
        "state": "unknown",
    }


def test_device_activity_provider_rejects_negative_threshold():
    try:
        DeviceActivityContextProvider(active_threshold_seconds=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("Negative activity threshold should be rejected")
