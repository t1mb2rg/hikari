from conversation.cli import default_context_collector


def test_direct_chat_hides_raw_desktop_activity_by_default():
    names = [provider.name for provider in default_context_collector().providers]

    assert "time" in names
    assert "host" in names
    assert "foreground" not in names
    assert "input_activity" not in names


def test_direct_chat_can_explicitly_enable_desktop_activity_context():
    names = [
        provider.name
        for provider in default_context_collector(include_desktop_activity=True).providers
    ]

    assert "foreground" in names
    assert "input_activity" in names
