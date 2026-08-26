from events import Event, Sensor, SensorRunner


class FakeSensor:
    name = "fake"

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def poll(self) -> list[Event]:
        self.calls += 1
        return [
            Event(
                event_type="fake.observation",
                source=self.name,
                content=self.content,
            )
        ]


def test_sensor_contract_accepts_interchangeable_sensor():
    sensor = FakeSensor("seen")
    assert isinstance(sensor, Sensor)


def test_runner_collects_events_from_multiple_sensors():
    first = FakeSensor("first")
    second = FakeSensor("second")

    events = SensorRunner([first, second]).poll_once()

    assert [event.content for event in events] == ["first", "second"]
    assert first.calls == 1
    assert second.calls == 1


def test_runner_forwards_events_without_knowing_sensor_type():
    received: list[Event] = []
    runner = SensorRunner([FakeSensor("forwarded")], on_event=received.append)

    events = runner.poll_once()

    assert received == events
    assert received[0].source == "fake"
