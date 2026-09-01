"""A user's daily weather, for messa/briefings.py -- deliberately the
SAME provider timeutil.py's city/zip geocoding already uses (Open-Meteo:
free, keyless, no signup), so this project has exactly one third-party
weather dependency instead of evaluating a second service. See
config.py's "Weather" section for the free-tier numbers this was checked
against.

Explicit product decision, not a fallback chain: if the request fails, times
out, or comes back with a reading outside any plausible real-world range,
`get_daily_weather` returns None and the caller drops the weather line
entirely rather than ever show a wrong, stale, or made-up number. There is
no second weather provider to fail over to -- reliability here means "don't
show it if it's not trustworthy," not "try harder to show something."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from . import config

# The subset of WMO weather interpretation codes Open-Meteo's `weather_code`
# field actually returns (documented at open-meteo.com/en/docs). Every code
# in this range is covered so an unmapped code can only mean the API added
# something new -- handled by _describe_code's fallback below, not a KeyError.
_WMO_DESCRIPTIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy (freezing fog)",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}

# Sanity bounds a genuinely correct reading could never fall outside --
# world extremes are roughly -128F (Antarctica) / 134F (Death Valley), so
# these leave real margin on both sides. A response outside this range is
# far more likely a unit mix-up or a garbled API response than a real
# temperature, and either way isn't something to text a user as-is.
_MIN_PLAUSIBLE_TEMP_F = -90.0
_MAX_PLAUSIBLE_TEMP_F = 135.0


def _describe_code(code: int | None) -> str:
    if code is None:
        return "conditions unclear"
    return _WMO_DESCRIPTIONS.get(code, "mixed conditions")


@dataclass
class DayWeather:
    date: str  # "YYYY-MM-DD", in the location's own local calendar (Open-Meteo's `timezone` param)
    high: float
    low: float
    description: str
    precip_probability: int | None  # 0-100, None if Open-Meteo didn't return one for this day
    unit: str  # "F" or "C", matching config.WEATHER_TEMPERATURE_UNIT

    def one_liner(self) -> str:
        """A compact single line for a briefing -- e.g. "72°/58°F, partly
        cloudy, 20% rain" -- with the rain-chance clause only present when
        Open-Meteo actually returned one and it's non-trivial (skipping a
        "0% rain" clause on a clear day keeps this shorter, per the
        "short but still has the details" ask -- a near-zero chance isn't
        a detail worth a whole clause)."""
        temps = f"{round(self.high)}°/{round(self.low)}°{self.unit}"
        rain = (
            f", {self.precip_probability}% rain"
            if self.precip_probability is not None and self.precip_probability >= 10
            else ""
        )
        return f"{temps}, {self.description}{rain}"


def _is_plausible(day: dict[str, Any]) -> bool:
    high, low = day.get("temperature_2m_max"), day.get("temperature_2m_min")
    if high is None or low is None:
        return False
    if not (_MIN_PLAUSIBLE_TEMP_F <= high <= _MAX_PLAUSIBLE_TEMP_F):
        return False
    if not (_MIN_PLAUSIBLE_TEMP_F <= low <= _MAX_PLAUSIBLE_TEMP_F):
        return False
    if low > high:  # a real forecast never reports a low above its own high
        return False
    return True


async def get_daily_weather(
    latitude: float, longitude: float, timezone_name: str, *, days: int | None = None,
) -> list[DayWeather] | None:
    """Fetches `days` (default config.WEATHER_FORECAST_DAYS) days of daily
    high/low/condition/rain-chance starting today, in the LOCATION's own
    local calendar (Open-Meteo's `timezone` param -- so "today" means the
    user's today, not a UTC day boundary that could be off by one
    depending on the hour this happens to run). Returns None -- never a
    partial or best-guess list -- on any network failure, unexpected
    response shape, or an implausible reading on ANY requested day: a
    forecast that's broken for tomorrow isn't safe to trust for today
    either, since both come from the same response.

    The Fahrenheit rounding above happens in DayWeather.one_liner, not
    here -- this function keeps the raw floats Open-Meteo returned."""
    day_count = days if days is not None else config.WEATHER_FORECAST_DAYS
    unit_label = "F" if config.WEATHER_TEMPERATURE_UNIT == "fahrenheit" else "C"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": config.WEATHER_TEMPERATURE_UNIT,
        "timezone": timezone_name,
        "forecast_days": day_count,
    }
    try:
        async with httpx.AsyncClient(timeout=config.WEATHER_TIMEOUT_SECONDS) as client:
            resp = await client.get(config.WEATHER_FORECAST_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 - any failure here means "drop the weather line," never a crash
        return None

    daily = data.get("daily")
    if not isinstance(daily, dict):
        return None
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    precip = daily.get("precipitation_probability_max") or []
    if not (len(dates) == len(highs) == len(lows) == len(codes)):
        return None  # a malformed/partial response -- don't guess at alignment between arrays

    results: list[DayWeather] = []
    for i, date in enumerate(dates):
        row = {"temperature_2m_max": highs[i], "temperature_2m_min": lows[i]}
        if not _is_plausible(row):
            return None  # one bad day taints the whole response -- see docstring
        results.append(DayWeather(
            date=date,
            high=highs[i],
            low=lows[i],
            description=_describe_code(codes[i]),
            precip_probability=precip[i] if i < len(precip) else None,
            unit=unit_label,
        ))
    return results or None
