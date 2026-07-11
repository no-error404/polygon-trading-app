"""
Weather Client — Open-Meteo multi-model ensemble forecasts.

Fetches temperature forecasts from multiple weather models via the
free, no-auth Open-Meteo API. Returns ensemble runs for a given
station coordinate.

Models queried:
  - gfs_seamless     (NOAA GFS, global, medium range)
  - ecmwf_seamless   (European ECMWF, best 3-7d skill)
  - icon_seamless    (German ICON, global)
  - metno_seamless   (MET Norway)

CRITICAL: We query the EXACT station coordinates (from station_lookup),
NOT the city centroid. A 10km offset can shift the max temp by 2-3°F
and flip the bracket.
"""

import json
import urllib.request
import urllib.parse
import urllib.error
import time
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
from functools import lru_cache
import hashlib

# --- In-process TTL cache for ensemble fetches ---
# Deduplicates requests when highest/lowest markets share the same station+date.
# TTL = 120s (well within a 300s cycle, but fresh enough for new cycle data).
_CACHE_TTL = 120
_ensemble_cache = {}  # key -> (timestamp, EnsembleResult)

def _cache_key(lat, lon, target_date, models, units):
    mhash = hashlib.md5("|".join(models).encode()).hexdigest()[:8]
    return f"{lat:.4f},{lon:.4f},{target_date},{mhash},{units}"


OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"


@dataclass
class ForecastResult:
    """Forecast for a station on a target date."""
    station_icao: str
    lat: float
    lon: float
    target_date: str  # YYYY-MM-DD
    model: str
    # Hourly temperature data for the target day
    hourly_times: list = field(default_factory=list)
    hourly_temps: list = field(default_factory=list)
    max_temp: float = 0.0
    min_temp: float = 0.0
    units: str = "F"  # "F" or "C"

    def __post_init__(self):
        if self.hourly_temps:
            self.max_temp = max(self.hourly_temps)
            self.min_temp = min(self.hourly_temps)


@dataclass
class EnsembleResult:
    """Multi-model ensemble result for a station."""
    station_icao: str
    target_date: str
    models: dict = field(default_factory=dict)  # model_name -> ForecastResult
    max_temps_all: list = field(default_factory=list)  # all model max temps
    min_temps_all: list = field(default_factory=list)  # all model min temps
    units: str = "F"

    def consensus(self):
        """Return mean and stdev of max temps across models."""
        if not self.max_temps_all:
            return 0.0, 0.0
        n = len(self.max_temps_all)
        mean = sum(self.max_temps_all) / n
        variance = sum((t - mean) ** 2 for t in self.max_temps_all) / n
        return mean, variance ** 0.5

    def consensus_min(self):
        """Return mean and stdev of min temps across models."""
        if not self.min_temps_all:
            return 0.0, 0.0
        n = len(self.min_temps_all)
        mean = sum(self.min_temps_all) / n
        variance = sum((t - mean) ** 2 for t in self.min_temps_all) / n
        return mean, variance ** 0.5


def _get(url: str, timeout: int = 15, retries: int = 3) -> dict:
    """GET with retry. Returns parsed JSON.

    Respects 429 rate limits with exponential backoff up to 60s.
    Small delay between requests to stay under Open-Meteo's free-tier limit.

    CIRCUIT BREAKER: if we get 429 on the first attempt, skip remaining retries
    for this model — retrying during an IP-level ban only extends the ban.
    """
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "quantmet-weather-trader/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_err = str(e)
            if e.code == 429:
                if attempt == 0:
                    # Circuit breaker: don't retry during IP-level ban
                    raise ConnectionError(f"Open-Meteo 429 (circuit breaker): {last_err}")
                wait = min(10 * (3 ** attempt), 60)
                time.sleep(wait)
            else:
                time.sleep(2 ** attempt)
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    raise ConnectionError(f"Open-Meteo failed after {retries} retries: {last_err}")


def fetch_forecast(
    lat: float,
    lon: float,
    target_date: str,
    model: str = "gfs_seamless",
    units: str = "F",
) -> ForecastResult:
    """
    Fetch a single model forecast for a station coordinate.

    Args:
        lat, lon: station coordinates
        target_date: YYYY-MM-DD
        model: Open-Meteo model identifier
        units: "F" or "C" (Open-Meteo returns Celsius, we convert)

    Returns:
        ForecastResult with hourly temps for the target day
    """
    # Open-Meteo always returns Celsius; we convert to F if needed
    temp_unit = "celsius"

    # Build the URL
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": model,
        "timezone": "auto",
        "start_date": target_date,
        "end_date": target_date,
        "temperature_unit": temp_unit,
    }
    url = f"{OPEN_METEO_FORECAST}?{urllib.parse.urlencode(params)}"

    data = _get(url)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps_c = hourly.get("temperature_2m", [])

    # Convert to requested units
    if units == "F":
        temps = [c * 9 / 5 + 32 if c is not None else None for c in temps_c]
    else:
        temps = temps_c

    result = ForecastResult(
        station_icao="",
        lat=lat,
        lon=lon,
        target_date=target_date,
        model=model,
        hourly_times=times,
        hourly_temps=[t for t in temps if t is not None],
        units=units,
    )
    return result


def fetch_ensemble(
    lat: float,
    lon: float,
    target_date: str,
    models: list = None,
    units: str = "F",
) -> EnsembleResult:
    """
    Fetch multi-model ensemble for a station.

    Queries each model separately (Open-Meteo free tier doesn't support
    multi-model in one call) and aggregates.

    Args:
        lat, lon: station coordinates
        target_date: YYYY-MM-DD
        models: list of Open-Meteo model IDs
        units: "F" or "C"

    Returns:
        EnsembleResult with per-model forecasts and aggregated max temps
    """
    if models is None:
        # Reduced from 6 to 3 models to stay under Open-Meteo free-tier rate limits.
        # These 3 give good global coverage with 50% fewer API calls.
        models = [
            "gfs_seamless",     # NOAA GFS — global, medium range
            "ecmwf_ifs025",     # European ECMWF — best 3-7d skill
            "icon_seamless",     # German ICON — global
        ]

    # --- TTL cache: deduplicate requests for same station+date+models ---
    # When highest+lowest markets share the same station, we fetch once.
    key = _cache_key(lat, lon, target_date, models, units)
    now = time.time()
    cached = _ensemble_cache.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    result = EnsembleResult(
        station_icao="",
        target_date=target_date,
        units=units,
    )

    for model in models:
        try:
            forecast = fetch_forecast(lat, lon, target_date, model, units)
            forecast.station_icao = result.station_icao
            result.models[model] = forecast
            if forecast.max_temp:
                result.max_temps_all.append(forecast.max_temp)
            if forecast.min_temp:
                result.min_temps_all.append(forecast.min_temp)
        except Exception as e:
            # One model failing shouldn't kill the ensemble
            print(f"[WARN] model {model} failed: {e}")
            continue

    # Cache the result — but ONLY if we got at least 1 model.
    # Caching empty results (all 429s) would poison the cache for 120s
    # even after the API recovers.
    if result.models:
        _ensemble_cache[key] = (now, result)
    return result


def fetch_station_forecast(
    station,
    target_date: str,
    models: list = None,
) -> EnsembleResult:
    """
    Fetch ensemble forecast for a Station object.

    Args:
        station: Station dataclass from station_lookup
        target_date: YYYY-MM-DD
        models: list of model IDs (default: 4 standard models)

    Returns:
        EnsembleResult with station_icao set
    """
    if models is None:
        # Reduced from 6 to 3 models to stay under Open-Meteo free-tier rate limits.
        models = [
            "gfs_seamless",     # NOAA GFS
            "ecmwf_ifs025",     # European ECMWF (0.25° resolution)
            "icon_seamless",     # German ICON
        ]

    result = fetch_ensemble(station.lat, station.lon, target_date, models, station.units)
    result.station_icao = station.icao
    return result


if __name__ == "__main__":
    print("Weather Client Self-Test")
    print("=" * 60)

    # Test: fetch forecast for KLGA (LaGuardia) for today
    from src.data.station_lookup import StationLookup

    lookup = StationLookup("config/stations.yaml")
    klga = lookup.get_station("KLGA")
    print(f"\nStation: {klga.icao} ({klga.name})")
    print(f"  lat={klga.lat}, lon={klga.lon}, units={klga.units}")

    # Use today's date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"  target_date={today}")

    print("\n  Fetching multi-model ensemble...")
    result = fetch_station_forecast(klga, today)

    print(f"\n  Models fetched: {len(result.models)}")
    for model, forecast in result.models.items():
        print(f"    {model}: max={forecast.max_temp:.1f}{klga.units} min={forecast.min_temp:.1f}{klga.units}")

    mean, stdev = result.consensus()
    print(f"\n  Consensus: mean={mean:.1f}{klga.units}, stdev={stdev:.1f}{klga.units}")
    print(f"  All max temps: {[f'{t:.1f}' for t in result.max_temps_all]}")