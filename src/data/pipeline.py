"""
pipeline.py — Async orchestrator that pairs market data with weather forecasts.

This is the unified Data Ingestion Pipeline. It runs both data sources
concurrently via asyncio, then outputs a single Pandas DataFrame where
each row pairs a contract's implied probability (market price) with the
scientific model's forecast for that bucket.

DataFrame Schema (one row per bracket bucket per event):
  event_slug        str     Polymarket event slug
  event_title       str     Human-readable event title
  station_icao      str     Airport ICAO code (resolution target)
  station_lat       float   Station latitude
  station_lon       float   Station longitude
  target_date       str     ISO date of the weather observation
  bucket_label      str     Temperature bracket label ("86-87", "<=77")
  bucket_low        float   Bracket lower bound
  bucket_high       float   Bracket upper bound
  units             str     "F" or "C"
  token_id          str     CLOB YES token ID
  condition_id      str     Polymarket condition ID
  best_bid          float   Highest bid price (market buy price for NO)
  best_ask          float   Lowest ask price (market sell price for YES)
  spread            float   Bid-ask spread
  midpoint         float   Midpoint price
  implied_prob      float   Normalised implied probability (YES)
  bid_depth         float   Total size at top-3 bid levels
  ask_depth         float   Total size at top-3 ask levels
  volume            float   Market volume (USDC)
  model_name        str     NWP model name (gfs_seamless, ecmwf, ...)
  model_temp_c      float   Model's predicted max temp (Celsius)
  model_temp_mkt    float   Model's predicted max temp (market units)
  model_in_bucket   bool    Whether the model's prediction falls in this bucket
  ensemble_size     int     Number of models fetched
  fetched_at        str     ISO timestamp of the fetch

When multiple models are fetched, each model gets its own row for the same
bucket. This "long format" lets the strategy layer compute per-model
disagreement and ensemble probabilities.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .async_gamma_client import AsyncGammaClient, WeatherEventMarket
from .async_weather_client import AsyncWeatherClient, EnsembleForecast
from .station_lookup import StationLookup, Station, StationLookupError
from .rate_limiter import gamma_rate_gate, clob_rate_gate, weather_rate_gate
from .cache import GAMMA_CACHE, CLOB_CACHE, WEATHER_CACHE, TTLCache

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised when the data ingestion pipeline fails critically."""


class DataIngestionPipeline:
    """
    Orchestrates concurrent fetching from Polymarket and Open-Meteo APIs,
    producing a unified Pandas DataFrame pairing market prices with forecasts.
    """

    def __init__(
        self,
        station_lookup: Optional[StationLookup] = None,
        gamma_timeout: float = 30.0,
        weather_timeout: float = 30.0,
        forecast_days: int = 14,
    ) -> None:
        self.station_lookup = station_lookup or StationLookup()
        self.gamma_timeout = gamma_timeout
        self.weather_timeout = weather_timeout
        self.forecast_days = forecast_days

    async def run(
        self,
        active_only: bool = False,
    ) -> pd.DataFrame:
        """
        Execute the full data ingestion pipeline:

        1. Discover weather events from Gamma (concurrent with weather fetch)
        2. Fetch all bracket orderbooks from CLOB (concurrent per bucket)
        3. Resolve station ICAO for each event from description text
        4. Fetch multi-model forecast for each station (concurrent per model)
        5. Merge into unified DataFrame

        Returns a DataFrame in the schema documented at the top of this file.
        """
        async with AsyncGammaClient(timeout=self.gamma_timeout) as gamma, \
                   AsyncWeatherClient(timeout=self.weather_timeout) as weather:

            # Step 1: Discover events
            events = await gamma.discover_weather_events(active_only=active_only)
            if not events:
                logger.info("No weather events found")
                return pd.DataFrame()

            # Step 2: Fetch orderbooks for all events concurrently
            events = await asyncio.gather(
                *(gamma.fetch_event_orderbooks(e) for e in events),
                return_exceptions=True,
            )
            valid_events: list[WeatherEventMarket] = []
            for e in events:
                if isinstance(e, WeatherEventMarket):
                    valid_events.append(e)
                elif isinstance(e, Exception):
                    logger.warning(f"Event orderbook fetch failed: {e}")

            if not valid_events:
                logger.warning("No events with valid orderbooks")
                return pd.DataFrame()

            # Step 3: Resolve stations + Step 4: Fetch forecasts concurrently
            # Each event gets its station resolved, then weather fetched.
            # For past target dates (closed markets), use the archive API;
            # for future dates (active markets), use the forecast API.
            from ..strategy.market_mapper import extract_target_date
            from datetime import datetime, date

            async def resolve_and_fetch(
                event: WeatherEventMarket,
            ) -> tuple[WeatherEventMarket, Station, EnsembleForecast]:
                # Resolve station from description
                station = self.station_lookup.resolve_market(
                    event.event_description,
                    event.event_title,
                )

                # Determine target date and whether it's in the past
                target_date_str = extract_target_date(event.event_title)
                today = date.today()

                if target_date_str:
                    target_date = date.fromisoformat(target_date_str)
                    if target_date < today:
                        # Past date — use historical archive for backtesting
                        # Fetch a 3-day window around the target date
                        from datetime import timedelta
                        start = (target_date - timedelta(days=2)).isoformat()
                        end = (target_date + timedelta(days=2)).isoformat()
                        forecast = await weather.get_historical(
                            lat=station.lat,
                            lon=station.lon,
                            start_date=start,
                            end_date=end,
                            station_icao=station.icao,
                        )
                    else:
                        # Future date — use forecast API
                        forecast = await weather.get_station_forecast(
                            station, forecast_days=self.forecast_days
                        )
                else:
                    # No date parsed — use forecast as default
                    forecast = await weather.get_station_forecast(
                        station, forecast_days=self.forecast_days
                    )

                return event, station, forecast

            # Run all station+forecast fetches concurrently
            resolved = await asyncio.gather(
                *(resolve_and_fetch(e) for e in valid_events),
                return_exceptions=True,
            )

            # Step 5: Build unified DataFrame
            rows: list[dict] = []
            for result in resolved:
                if isinstance(result, Exception):
                    logger.warning(f"Station/forecast resolution failed: {result}")
                    continue

                event, station, forecast = result
                event_rows = self._build_rows(event, station, forecast)
                rows.extend(event_rows)

            if not rows:
                logger.warning("No data rows produced")
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            logger.info(f"Pipeline produced {len(df)} rows "
                        f"from {len(valid_events)} events")
            return df

    def _build_rows(
        self,
        event: WeatherEventMarket,
        station: Station,
        forecast: EnsembleForecast,
    ) -> list[dict]:
        """
        Build DataFrame rows for one event.
        Each bracket bucket × each model = one row (long format).
        """
        rows: list[dict] = []

        # Determine target date from event title
        from ..strategy.market_mapper import extract_target_date, parse_bucket, detect_units
        target_date = extract_target_date(event.event_title)
        units = detect_units(event.event_title)

        # Normalised implied probabilities
        yes_prices = [p[0] if p else 0.0 for p in event.outcome_prices]
        total_prob = sum(yes_prices) if sum(yes_prices) > 0 else 1.0
        normalised_probs = [p / total_prob for p in yes_prices]

        # Ensemble temps for the target date (in market units)
        ensemble_temps_c: dict[str, float] = forecast.get_day_temps(target_date) \
            if target_date else {}

        fetched_at = datetime.now(timezone.utc).isoformat()

        for i, contract in enumerate(event.contracts):
            if i >= len(event.bucket_labels):
                break

            bucket_label = event.bucket_labels[i]
            try:
                bucket = parse_bucket(bucket_label)
            except ValueError:
                logger.warning(f"Cannot parse bucket '{bucket_label}', skipping")
                continue

            implied_prob = (normalised_probs[i]
                            if i < len(normalised_probs) else 0.0)

            # If we have ensemble forecasts, emit one row per model
            if ensemble_temps_c:
                for model_name, temp_c in ensemble_temps_c.items():
                    # Convert to market units
                    temp_mkt = (temp_c * 9.0 / 5.0 + 32.0
                                if units == "F" else temp_c)
                    in_bucket = bucket.contains(temp_mkt)

                    rows.append({
                        "event_slug": event.event_slug,
                        "event_title": event.event_title,
                        "station_icao": station.icao,
                        "station_lat": station.lat,
                        "station_lon": station.lon,
                        "target_date": target_date or "",
                        "bucket_label": bucket_label,
                        "bucket_low": bucket.low,
                        "bucket_high": bucket.high,
                        "units": units,
                        "token_id": contract.token_id,
                        "condition_id": contract.condition_id,
                        "best_bid": contract.best_bid,
                        "best_ask": contract.best_ask,
                        "spread": contract.spread,
                        "midpoint": contract.midpoint,
                        "implied_prob": implied_prob,
                        "bid_depth": contract.bid_depth,
                        "ask_depth": contract.ask_depth,
                        "volume": contract.volume,
                        "model_name": model_name,
                        "model_temp_c": temp_c,
                        "model_temp_mkt": temp_mkt,
                        "model_in_bucket": in_bucket,
                        "ensemble_size": len(ensemble_temps_c),
                        "fetched_at": fetched_at,
                    })
            else:
                # No forecast data available — still emit market data rows
                rows.append({
                    "event_slug": event.event_slug,
                    "event_title": event.event_title,
                    "station_icao": station.icao,
                    "station_lat": station.lat,
                    "station_lon": station.lon,
                    "target_date": target_date or "",
                    "bucket_label": bucket_label,
                    "bucket_low": bucket.low,
                    "bucket_high": bucket.high,
                    "units": units,
                    "token_id": contract.token_id,
                    "condition_id": contract.condition_id,
                    "best_bid": contract.best_bid,
                    "best_ask": contract.best_ask,
                    "spread": contract.spread,
                    "midpoint": contract.midpoint,
                    "implied_prob": implied_prob,
                    "bid_depth": contract.bid_depth,
                    "ask_depth": contract.ask_depth,
                    "volume": contract.volume,
                    "model_name": None,
                    "model_temp_c": None,
                    "model_temp_mkt": None,
                    "model_in_bucket": None,
                    "ensemble_size": 0,
                    "fetched_at": fetched_at,
                })

        return rows

    async def cache_stats(self) -> dict[str, dict[str, int]]:
        """Return cache hit/miss statistics for all caches."""
        return {
            "gamma": GAMMA_CACHE.stats(),
            "clob": CLOB_CACHE.stats(),
            "weather": WEATHER_CACHE.stats(),
        }


async def run_pipeline(active_only: bool = False) -> pd.DataFrame:
    """
    Convenience function: instantiate and run the pipeline in one call.

    Usage:
        import asyncio
        from src.data.pipeline import run_pipeline
        df = asyncio.run(run_pipeline())
    """
    pipeline = DataIngestionPipeline()
    return await pipeline.run(active_only=active_only)