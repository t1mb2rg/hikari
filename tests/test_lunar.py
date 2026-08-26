from datetime import date

from awareness import ChineseCalendarContextProvider, ContextCollector
from events import Event


def test_chinese_calendar_provider_converts_known_new_year_date():
    provider = ChineseCalendarContextProvider(
        date_provider=lambda: date(2020, 1, 25),
    )

    context = provider.capture()

    assert context["solar_date"] == "2020-01-25"
    assert context["lunar_year"] == 2020
    assert context["lunar_month"] == 1
    assert context["lunar_day"] == 1
    assert context["is_leap_month"] is False
    assert context["month_text"] == "正"
    assert context["day_text"] == "初一"
    assert context["date_text"] == "农历正月初一"
    assert context["zodiac"] == "鼠"
    assert "春节" in context["lunar_festivals"]


def test_chinese_calendar_context_can_be_attached_to_event():
    provider = ChineseCalendarContextProvider(
        date_provider=lambda: date(2020, 1, 25),
    )
    collector = ContextCollector([provider])

    enriched = collector.enrich(
        Event(
            event_type="test.date",
            source="fake",
            content="Date context test",
        )
    )

    chinese_calendar = enriched.context["_hikari_context"]["providers"]["chinese_calendar"]
    assert chinese_calendar["date_text"] == "农历正月初一"
    assert chinese_calendar["year_ganzhi"] == "庚子"
