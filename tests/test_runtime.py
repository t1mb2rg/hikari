from core.runtime import HikariRuntime, heartbeat


def test_heartbeat_signal():
    assert heartbeat() == "Hikari is awake."


def test_runtime_start_loads_identity_and_runs():
    runtime = HikariRuntime()

    message = runtime.start()

    assert runtime.identity is not None
    assert runtime.identity.name == "Hikari"
    assert runtime.running is True
    assert message == "Hikari is awake."


def test_runtime_stop_is_clean():
    runtime = HikariRuntime()
    runtime.start()

    runtime.stop()

    assert runtime.running is False
    assert runtime.identity is not None
