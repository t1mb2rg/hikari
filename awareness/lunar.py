from __future__ import annotations

from collections.abc import Callable
from datetime import date

from lunar_python import Solar


DateProvider = Callable[[], date]


class ChineseCalendarContextProvider:
    """Cheap local Chinese lunar-calendar context for the current date.

    Lunar date is ambient time context. It is deliberately independent from any
    calendar vendor or schedule backend.
    """

    name = "chinese_calendar"

    def __init__(self, *, date_provider: DateProvider = date.today) -> None:
        self.date_provider = date_provider

    def capture(self) -> dict[str, object]:
        today = self.date_provider()
        solar = Solar.fromYmd(today.year, today.month, today.day)
        lunar = solar.getLunar()

        lunar_month = int(lunar.getMonth())
        is_leap_month = lunar_month < 0
        month_text = lunar.getMonthInChinese()
        day_text = lunar.getDayInChinese()
        leap_prefix = "闰" if is_leap_month else ""

        jieqi = lunar.getJieQi()

        return {
            "solar_date": today.isoformat(),
            "lunar_year": int(lunar.getYear()),
            "lunar_month": abs(lunar_month),
            "lunar_day": int(lunar.getDay()),
            "is_leap_month": is_leap_month,
            "month_text": month_text,
            "day_text": day_text,
            "date_text": f"农历{leap_prefix}{month_text}月{day_text}",
            "year_ganzhi": lunar.getYearInGanZhi(),
            "zodiac": lunar.getYearShengXiao(),
            "jieqi": jieqi or None,
            "solar_festivals": list(solar.getFestivals()),
            "lunar_festivals": list(lunar.getFestivals()),
        }
