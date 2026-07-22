"""
async_weather_client.py — Async httpx client for Open-Meteo multi-model forecasts.

Fetches historical and forecasted meteorological variables (high/low temps,
precipitation) from multiple NWP models CONCURRENTLY via asyncio.

Each model has its own Open-Meteo endpoint (/v1/gfs, /v1/ecmwf, etc.).
We fetch all models in parallel, then aggregate into a unified structure.

Rate-limited and cached. Uses station ICAO coordinates, not city centroids.

Key features:
  - asyncio.gather for concurrent model fetches
  - Per-model endpoint with graceful degradation (model down -> skip)
  - Historical archive endpoint for backtesting
  - Unit conversion (API returns Celsius; caller specifies market units)
  - Structural type hints via Protocol
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

from .rate_limiter import weather_rate_gate
from .cache import WEATHER_CACHE

logger = logging.getLogger(__name__)

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"
OPEN_METEO_ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1"

# Each model has its own endpoint path
MODEL_ENDPOINTS: dict[str, str] = {
    "gfs_seamless":        "/gfs",
    "ecmwf":                "/ecmwf",
    "meteofrance_seamless": "/meteofrance",
    "gem_seamless":         "/gem",
    "jma_seamless":         "/jma",
    "metno_seamless":       "/metno",
    "kma_seamless":         "/kma",
}

DEFAULT_MODELS = [
    "gfs_seamless",
    "ecmwf",
    "meteofrance_seamless",
    "gem_seamless",
]

DAILY_VARS = (
    "temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,snowfall_sum"
)


# --- Structural type hints ---

@runtime_checkable
class WeatherStationLike(Protocol):
    """Structural type for any object with station coordinates."""
    icao: str
    lat: float
    lon: float
    units: str


# --- Data models ---

@dataclass
class ModelForecast:
    """A single model's forecast for a date range."""
    model: str
    dates: list[str]
    temp_max_c: list[float]
    temp_min_c: list[float]
    precip_mm: list[float]
    snowfall_cm: list[float]


@dataclass
class EnsembleForecast:
    """Aggregated multi-model forecast for a station."""
    station_icao: str
    lat: float
    lon: float
    fetched_at: str
    models: list[ModelForecast] = field(default_factory=list)

    def get_day_temps(self, target_date: str) -> dict[str, float]:
        """{model_name: max_temp_c} for a specific date."""
        result: dict[str, float] = {}
        for mf in self.models:
            if target_date in mf.dates:
                idx = mf.dates.index(target_date)
                result[mf.model] = mf.temp_max_c[idx]
        return result

    def get_ensemble_temps_c(self, target_date: str) -> list[float]:
        """Flat list of all model max-temp predictions (Celsius)."""
        return list(self.get_day_temps(target_date).values())

    def get_ensemble_temps_f(self, target_date: str) -> list[float]:
        """Flat list of all model max-temp predictions (Fahrenheit)."""
        return [c * 9.0 / 5.0 + 32.0
                for c in self.get_ensemble_temps_c(target_date)]


# --- Client ---

class AsyncWeatherClient:
    """
    Async httpx client for Open-Meteo multi-model forecasts.

    Fetches all models concurrently, with rate limiting and caching.
    Supports both forecast (future) and archive (historical) endpoints.
    """

    def __init__(
        self,
        base_url: str = OPEN_METEO_BASE,
        archive_base_url: str = OPEN_METEO_ARCHIVE_BASE,
        timeout: float = 30.0,
        max_concurrent_models: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.archive_base_url = archive_base_url.rstrip("/")
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent_models)

    async def __aenter__(self) -> AsyncWeatherClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": "polymarket-weather-trader/2.0",
                     "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._client.aclose()

    async def _fetch_model_forecast(
        self,
        model_name: str,
        endpoint: str,
        lat: float,
        lon: float,
        forecast_days: int,
        base_url: str,
    ) -> Optional[ModelForecast]:
        """Fetch a single model's forecast. Returns None on error."""
        url = f"{base_url}{endpoint}"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": DAILY_VARS,
            "timezone": "auto",
            "forecast_days": forecast_days,
        }

        async with self._semaphore:
            async def _fetch() -> dict:
                await weather_rate_gate()
                resp = await self._client.get(url, params=params,
                                             timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()

            cache_key = f"weather:{url}:{lat},{lon}:{forecast_days}"
            try:
                data = await WEATHER_CACHE.get_or_set(
                    cache_key, _fetch, ttl_seconds=3600
                )
            except httpx.HTTPStatusError as e:
                logger.warning(f"Model {model_name} HTTP error: {e}")
                return None
            except httpx.RequestError as e:
                logger.warning(f"Model {model_name} network error: {e}")
                return None

        daily = data.get("daily", {})
        if not daily:
            return None

        dates = daily.get("time", [])
        n = len(dates)

        def _pad(lst: list, n: int) -> list:
            return lst[:n] + [0.0] * max(0, n - len(lst))

        return ModelForecast(
            model=model_name,
            dates=dates,
            temp_max_c=_pad(daily.get("temperature_2m_max", []), n),
            temp_min_c=_pad(daily.get("temperature_2m_min", []), n),
            precip_mm=_pad(daily.get("precipitation_sum", []), n),
            snowfall_cm=_pad(daily.get("snowfall_sum", []), n),
        )

    async def get_forecast(
        self,
        lat: float,
        lon: float,
        station_icao: str = "",
        models: Optional[list[str]] = None,
        forecast_days: int = 14,
    ) -> EnsembleForecast:
        """
        Fetch multi-model forecast concurrently.

        All model endpoints are hit in parallel via asyncio.gather.
        Failed models are skipped (graceful degradation).
        """
        if models is None:
            models = DEFAULT_MODELS

        tasks: list[asyncio.Task] = []
        for model_name in models:
            endpoint = MODEL_ENDPOINTS.get(model_name)
            if endpoint is None:
                logger.warning(f"Unknown model '{model_name}', skipping")
                continue
            tasks.append(asyncio.ensure_future(
                self._fetch_model_forecast(
                    model_name, endpoint, lat, lon, forecast_days,
                    self.base_url,
                )
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        ensemble = EnsembleForecast(
            station_icao=station_icao,
            lat=lat,
            lon=lon,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

        for result in results:
            if isinstance(result, ModelForecast):
                ensemble.models.append(result)
            elif isinstance(result, Exception):
                logger.warning(f"Model fetch failed: {result}")

        return ensemble

    async def get_station_forecast(
        self,
        station: WeatherStationLike,
        models: Optional[list[str]] = None,
        forecast_days: int = 14,
    ) -> EnsembleForecast:
        """
        Fetch forecast for a station object (structural typing).

        Accepts any object with .icao, .lat, .lon, .units attributes.
        Uses the station's exact coordinates — never a city centroid.
        """
        return await self.get_forecast(
            lat=station.lat,
            lon=station.lon,
            station_icao=station.icao,
            models=models,
            forecast_days=forecast_days,
        )

    async def get_historical(
        self,
        lat: float,
        lon: float,
        start_date: str,
        end_date: str,
        station_icao: str = "",
    ) -> EnsembleForecast:
        """
        Fetch historical observed weather from Open-Meteo archive API.

        Used for backtesting against closed Polymarket markets.
        Uses the ERA5 reanalysis model (best historical quality).
        """
        url = f"{self.archive_base_url}/era5"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": DAILY_VARS,
            "timezone": "auto",
            "start_date": start_date,
            "end_date": end_date,
        }

        async def _fetch() -> dict:
            await weather_rate_gate()
            resp = await self._client.get(url, params=params,
                                         timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()

        cache_key = f"archive:{lat},{lon}:{start_date}:{end_date}"
        try:
            data = await WEATHER_CACHE.get_or_set(
                cache_key, _fetch, ttl_seconds=86400  # 24h TTL for historical
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Archive API error: {e}")
            return EnsembleForecast(station_icao, lat, lon,
                                    datetime.now(timezone.utc).isoformat())
        except httpx.RequestError as e:
            logger.error(f"Archive network error: {e}")
            return EnsembleForecast(station_icao, lat, lon,
                                    datetime.now(timezone.utc).isoformat())

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        n = len(dates)

        def _pad(lst: list, n: int) -> list:
            return lst[:n] + [0.0] * max(0, n - len(lst))

        ensemble = EnsembleForecast(
            station_icao=station_icao,
            lat=lat,
            lon=lon,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        ensemble.models.append(ModelForecast(
            model="era5_reanalysis",
            dates=dates,
            temp_max_c=_pad(daily.get("temperature_2m_max", []), n),
            temp_min_c=_pad(daily.get("temperature_2m_min", []), n),
            precip_mm=_pad(daily.get("precipitation_sum", []), n),
            snowfall_cm=_pad(daily.get("snowfall_sum", []), n),
        ))

        return ensemble