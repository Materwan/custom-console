"""Location and weather tools (public HTTP APIs, no key needed)."""

from typing import Any, Callable, Dict, List, Literal, Optional

import requests

from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
LOCATION_URL = "https://ipinfo.io/json"
MAX_FORECAST_DAYS = 16
REQUEST_TIMEOUT = 10

DEFAULT_WEATHER_VARIABLES: Dict[str, List[str]] = {
    "current": [
        "temperature_2m",
        "apparent_temperature",
        "relative_humidity_2m",
        "wind_speed_10m",
        "weather_code",
        "precipitation",
    ],
    "hourly": [
        "temperature_2m",
        "precipitation_probability",
        "precipitation",
        "wind_speed_10m",
        "weather_code",
    ],
    "daily": [
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "weather_code",
        "wind_speed_10m_max",
    ],
}


def compact_weather(payload: Dict[str, Any], resolution: str) -> Dict[str, Any]:
    """Keep only the requested block of an Open-Meteo answer, floats rounded to
    one decimal: the raw payload (units, elevation, one array per variable...)
    is needlessly large for a language model."""
    block = payload.get(resolution)
    if block is None:
        return payload  # unexpected format: do not filter blindly

    def rounded(value: Any) -> Any:
        return round(value, 1) if isinstance(value, float) else value

    cleaned = {
        key: [rounded(v) for v in value] if isinstance(value, list) else rounded(value)
        for key, value in block.items()
    }
    return {resolution: cleaned, "timezone": payload.get("timezone")}


def web_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    @guarded(ctx, PermissionLevel.READ)
    def get_location() -> ToolResult:
        """Get the approximate location of this device (from its IP address).
        Returns city, region, country and "loc" (latitude,longitude)."""
        response = requests.get(LOCATION_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        return ToolResult.ok({key: data.get(key) for key in ("city", "region", "country", "loc")})

    @guarded(ctx, PermissionLevel.READ)
    def get_weather(
        latitude: float,
        longitude: float,
        resolution: Literal["current", "hourly", "daily"] = "current",
        variables: Optional[List[str]] = None,
        forecast_days: int = 7,
        timezone: str = "auto",
    ) -> ToolResult:
        """Get the weather for a position from Open-Meteo.

        Args:
            latitude: latitude in degrees.
            longitude: longitude in degrees.
            resolution: "current", "hourly" or "daily".
            variables: Open-Meteo variable names; default: a sensible set.
            forecast_days: 1-16 (ignored for "current").
            timezone: e.g. "Europe/Paris", or "auto".
        """
        if resolution not in DEFAULT_WEATHER_VARIABLES:
            raise ValueError(
                f"Unknown resolution {resolution!r}, expected one of "
                f"{', '.join(DEFAULT_WEATHER_VARIABLES)}."
            )
        params: Dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
            resolution: ",".join(variables or DEFAULT_WEATHER_VARIABLES[resolution]),
        }
        if resolution != "current":
            params["forecast_days"] = max(1, min(forecast_days, MAX_FORECAST_DAYS))

        response = requests.get(WEATHER_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return ToolResult.ok(compact_weather(response.json(), resolution))

    return [get_location, get_weather]
