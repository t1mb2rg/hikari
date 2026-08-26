import platform

from awareness.foreground import ForegroundContextProvider


def test_foreground_provider_exposes_window_metadata():
    provider = ForegroundContextProvider(
        foreground_reader=lambda: {
            "title": "Hikari - Visual Studio Code",
            "class_name": "Chrome_WidgetWin_1",
            "process_id": 4242,
        }
    )

    assert provider.capture() == {
        "supported": True,
        "available": True,
        "title": "Hikari - Visual Studio Code",
        "class_name": "Chrome_WidgetWin_1",
        "process_id": 4242,
    }


def test_foreground_provider_handles_missing_window(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    provider = ForegroundContextProvider(foreground_reader=lambda: None)

    assert provider.capture() == {
        "supported": True,
        "available": False,
    }


def test_foreground_provider_degrades_safely_when_unsupported(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    provider = ForegroundContextProvider(foreground_reader=lambda: None)

    assert provider.capture() == {
        "supported": False,
        "available": False,
    }
