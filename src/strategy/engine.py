"""
engine.py — WeatherStrategyEngine: the mathematical core of the system.

Consumes the unified DataFrame from the data ingestion pipeline (Phase 2)
and produces a DataFrame of tradeable opportunities with position sizing.

Three-stage pipeline inside the engine:
  1. PROBABILITY DIVERGENCE: ensemble forecast temps -> per-bucket p_model,
     then |p_model - p_market| divergence analysis.
  2. EV CALCULATOR: EV = p_model - ask_price per share, filtered by
     edge threshold (default EV > +0.05).
  3. FRACTIONAL KELLY SIZING: full Kelly -> quartered -> capped at
     5% single-market, liquidity-aware, total exposure capped.

Designed for unit testing: every method is pure (no I/O), takes explicit
inputs, and returns deterministic outputs. The only state is the config
loaded at construction time.

Usage:
    engine = WeatherStrategyEngine.from_config(settings)
    opportunities = engine.evaluate(df)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# --- Configuration ---

@dataclass(frozen=True)
class StrategyConfig:
    """All tunable parameters for the strategy engine."""
    # Probability divergence
    min_ensemble_size: int = 30          # shrink toward uniform below this
    calibration_alpha: float = 0.85     # p = alpha*p_ensemble + (1-alpha)*(1/N)
    prob_clip_min: float = 0.01         # prevent div-by-zero in Kelly
    prob_clip_max: float = 0.99

    # EV calculator
    min_ev_per_share: float = 0.05      # edge threshold (EV > +0.05)

    # Kelly sizing
    kelly_fraction: float = 0.25        # quarter-Kelly
    max_single_market_fraction: float = 0.05  # 5% of bankroll per contract
    max_total_exposure_fraction: float = 0.25 # 25% total open weather exposure
    min_bet_dollars: float = 1.00      # skip if final bet < $1 (gas not worth it)
    liquidity_depth_levels: int = 3    # top-N ask levels for liquidity cap

    # Bankroll
    bankroll: float = 1000.0

    @classmethod
    def from_yaml(cls, settings: dict) -> StrategyConfig:
        """Build from parsed settings.yaml dict."""
        strat = settings.get("strategy", {})
        risk = settings.get("risk", {})
        return cls(
            min_ensemble_size=strat.get("min_ensemble_size", 30),
            calibration_alpha=strat.get("calibration_alpha", 0.85),
            prob_clip_min=strat.get("prob_clip_min", 0.01),
            prob_clip_max=strat.get("prob_clip_max", 0.99),
            min_ev_per_share=strat.get("min_ev_per_share", 0.05),
            kelly_fraction=risk.get("kelly_fraction", 0.25),
            max_single_market_fraction=risk.get(
                "max_single_market_fraction", 0.05
            ),
            max_total_exposure_fraction=risk.get(
                "max_total_exposure_fraction", 0.25
            ),
            bankroll=risk.get("bankroll", 1000.0),
            liquidity_depth_levels=risk.get("liquidity_depth_levels", 3),
        )


# --- Result types ---

@dataclass
class Opportunity:
    """A single tradeable opportunity with full sizing."""
    event_slug: str
    station_icao: str
    target_date: str
    bucket_label: str
    bucket_index: int
    side: str                     # "YES" or "NO"
    units: str

    # Market state
    token_id: str
    condition_id: str
    implied_prob: float           # normalised market probability
    best_ask: Optional[float]
    best_bid: Optional[float]
    ask_price: float              # effective ask (for buys)

    # Model state
    p_model: float                # our calibrated probability
    p_market: float               # market implied (same as implied_prob)
    divergence: float             # p_model - p_market
    ensemble_size: int
    model_temps: list[float]

    # EV
    ev_per_share: float
    ev_pct: float                 # EV / cost

    # Kelly sizing
    f_star: float                 # full Kelly fraction
    f_fractional: float           # fractional Kelly fraction
    kelly_dollars: float
    single_market_cap: float
    liquidity_cap: float
    final_bet_dollars: float
    shares: float
    max_loss: float
    max_profit: float
    binding_cap: str              # "kelly" | "market" | "liquidity" | "none"
    skipped: bool
    skip_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "event_slug": self.event_slug,
            "station_icao": self.station_icao,
            "target_date": self.target_date,
            "bucket_label": self.bucket_label,
            "bucket_index": self.bucket_index,
            "side": self.side,
            "units": self.units,
            "token_id": self.token_id,
            "condition_id": self.condition_id,
            "implied_prob": self.implied_prob,
            "best_ask": self.best_ask,
            "best_bid": self.best_bid,
            "ask_price": self.ask_price,
            "p_model": self.p_model,
            "p_market": self.p_market,
            "divergence": self.divergence,
            "ensemble_size": self.ensemble_size,
            "ev_per_share": self.ev_per_share,
            "ev_pct": self.ev_pct,
            "f_star": self.f_star,
            "f_fractional": self.f_fractional,
            "kelly_dollars": self.kelly_dollars,
            "single_market_cap": self.single_market_cap,
            "liquidity_cap": self.liquidity_cap,
            "final_bet_dollars": self.final_bet_dollars,
            "shares": self.shares,
            "max_loss": self.max_loss,
            "max_profit": self.max_profit,
            "binding_cap": self.binding_cap,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


# --- Engine ---

class WeatherStrategyEngine:
    """
    Mathematical core: probability divergence -> EV -> fractional Kelly.

    Stateles after construction (config is frozen). Every method is pure
    and deterministic, making this class highly unit-testable.

    Usage:
        engine = WeatherStrategyEngine(config)
        opportunities = engine.evaluate(df)
        opp_df = engine.evaluate_dataframe(df)
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @classmethod
    def from_config(cls, settings: dict) -> WeatherStrategyEngine:
        """Build from parsed settings.yaml dict."""
        return cls(StrategyConfig.from_yaml(settings))

    # ---- Stage 1: Probability Divergence ----

    def compute_ensemble_probability(
        self,
        df: pd.DataFrame,
        bucket_label: str,
        bucket_low: float,
        bucket_high: float,
        units: str,
    ) -> tuple[float, int, list[float]]:
        """
        Compute p_model for a single bucket from ensemble forecast temps.

        Uses the DataFrame rows for one event+bucket that contain model
        forecast data. Returns (p_model, ensemble_size, model_temps).

        The probability is the fraction of ensemble members whose predicted
        max temp falls within [bucket_low, bucket_high], with calibration
        shrinkage toward uniform for small ensembles.
        """
        # Filter rows for this bucket that have model data
        bucket_rows = df[
            (df["bucket_label"] == bucket_label)
            & df["model_name"].notna()
        ]

        model_temps = list(bucket_rows["model_temp_mkt"].dropna())
        ensemble_size = len(model_temps)
        n_buckets = df["bucket_label"].nunique()

        if ensemble_size == 0 or n_buckets == 0:
            return 0.0, 0, []

        # Count how many ensemble members fall in this bucket
        # Each model temp is checked against the bucket bounds
        count = 0
        for temp in model_temps:
            if self._temp_in_bucket(temp, bucket_low, bucket_high):
                count += 1

        raw_prob = count / ensemble_size

        # Calibration shrinkage toward uniform (1/N)
        if ensemble_size < self.config.min_ensemble_size:
            alpha = self.config.calibration_alpha * (
                ensemble_size / self.config.min_ensemble_size
            )
        else:
            alpha = self.config.calibration_alpha

        uniform = 1.0 / n_buckets
        calibrated = alpha * raw_prob + (1 - alpha) * uniform

        # Clip to prevent Kelly division-by-zero
        calibrated = max(
            self.config.prob_clip_min,
            min(self.config.prob_clip_max, calibrated)
        )

        return calibrated, ensemble_size, model_temps

    @staticmethod
    def _temp_in_bucket(
        temp: float, low: float, high: float
    ) -> bool:
        """Check if temp falls in [low, high] (inclusive both bounds)."""
        if math.isinf(low):
            return temp <= high
        if math.isinf(high):
            return temp >= low
        return low <= temp <= high

    def compute_divergence(
        self, p_model: float, p_market: float
    ) -> float:
        """Absolute divergence between model and market probability."""
        return p_model - p_market

    # ---- Stage 2: EV Calculator ----

    def compute_ev(
        self, p_model: float, ask_price: float
    ) -> float:
        """
        EV per $1 share of YES.

        EV = p_model * (1 - ask) - (1 - p_model) * ask
           = p_model - ask

        For NO side: EV = (1 - p_model) - ask_no
        """
        return p_model - ask_price

    def find_edge(
        self,
        p_model: float,
        ask_yes: float,
        ask_no: Optional[float] = None,
    ) -> tuple[str, float, float]:
        """
        Check both YES and NO sides for positive EV.

        Returns (best_side, best_ev, best_ask_price).
        If neither side passes the threshold, returns ("SKIP", 0, 0).
        """
        ev_yes = self.compute_ev(p_model, ask_yes)

        if ask_no is None:
            # Infer NO ask conservatively: 1 - bid_yes
            # If no bid, use 1 - ask_yes (worst case for NO)
            ask_no = 1.0 - ask_yes

        ev_no = self.compute_ev(1.0 - p_model, ask_no)

        if ev_yes >= ev_no and ev_yes > self.config.min_ev_per_share:
            return "YES", ev_yes, ask_yes
        elif ev_no > ev_yes and ev_no > self.config.min_ev_per_share:
            return "NO", ev_no, ask_no
        return "SKIP", 0.0, 0.0

    # ---- Stage 3: Fractional Kelly Sizing ----

    def full_kelly_fraction(
        self, p_model: float, price: float
    ) -> float:
        """
        Full Kelly fraction for binary $0/$1 payout.

        f* = p - (1-p) * (price / (1-price))

        Returns 0 if price is at extremes (0 or 1).
        """
        if price <= 0 or price >= 1:
            return 0.0
        return p_model - (1.0 - p_model) * (price / (1.0 - price))

    def size_position(
        self,
        p_model: float,
        ask_price: float,
        ask_depth: float = 0.0,
        current_total_exposure: float = 0.0,
    ) -> dict:
        """
        Compute final position size with the three-cap cascade.

        Caps applied in order (smallest binding wins):
          1. Kelly $ = f_fractional * bankroll
          2. Single-market cap = max_single_market_fraction * bankroll
          3. Liquidity cap = ask_depth * ask_price (if ask_depth > 0)
          4. Total-exposure cap = remaining headroom under
             max_total_exposure_fraction * bankroll

        Returns a dict with all sizing details.
        """
        f_star = self.full_kelly_fraction(p_model, ask_price)

        if f_star <= 0:
            return {
                "f_star": f_star, "f_fractional": 0.0,
                "kelly_dollars": 0.0, "single_market_cap": 0.0,
                "liquidity_cap": 0.0, "total_exposure_cap": 0.0,
                "final_bet_dollars": 0.0, "shares": 0.0,
                "max_loss": 0.0, "max_profit": 0.0,
                "binding_cap": "none", "skipped": True,
                "skip_reason": f"No Kelly edge (f*={f_star:.4f} <= 0)",
            }

        f_frac = self.config.kelly_fraction * f_star
        kelly_dollars = f_frac * self.config.bankroll

        single_market_cap = (
            self.config.max_single_market_fraction * self.config.bankroll
        )

        if ask_depth > 0:
            liquidity_cap = ask_depth * ask_price
        else:
            liquidity_cap = float("inf")

        # Total exposure cap: remaining headroom
        max_total = (
            self.config.max_total_exposure_fraction * self.config.bankroll
        )
        remaining_headroom = max_total - current_total_exposure
        total_exposure_cap = max(0.0, remaining_headroom)

        # Binding cap = smallest
        candidates = [
            ("kelly", kelly_dollars),
            ("market", single_market_cap),
            ("liquidity", liquidity_cap),
            ("exposure", total_exposure_cap),
        ]
        binding_name, final_bet = min(candidates, key=lambda x: x[1])

        if final_bet < self.config.min_bet_dollars:
            return {
                "f_star": f_star, "f_fractional": f_frac,
                "kelly_dollars": kelly_dollars,
                "single_market_cap": single_market_cap,
                "liquidity_cap": liquidity_cap if liquidity_cap != float("inf") else 0.0,
                "total_exposure_cap": total_exposure_cap,
                "final_bet_dollars": 0.0, "shares": 0.0,
                "max_loss": 0.0, "max_profit": 0.0,
                "binding_cap": binding_name, "skipped": True,
                "skip_reason": f"Bet too small (${final_bet:.2f} < ${self.config.min_bet_dollars})",
            }

        shares = final_bet / ask_price if ask_price > 0 else 0.0
        max_payout = shares * 1.0
        max_profit = max_payout - final_bet

        return {
            "f_star": f_star, "f_fractional": f_frac,
            "kelly_dollars": kelly_dollars,
            "single_market_cap": single_market_cap,
            "liquidity_cap": liquidity_cap if liquidity_cap != float("inf") else 0.0,
            "total_exposure_cap": total_exposure_cap,
            "final_bet_dollars": final_bet, "shares": shares,
            "max_loss": final_bet, "max_profit": max_profit,
            "binding_cap": binding_name, "skipped": False,
            "skip_reason": "",
        }

    # ---- Full evaluation ----

    def evaluate(self, df: pd.DataFrame) -> list[Opportunity]:
        """
        Full strategy evaluation on a pipeline DataFrame.

        Steps per event:
          1. Group rows by event_slug
          2. For each bucket, compute ensemble p_model
          3. Compute divergence vs implied_prob
          4. Check both YES/NO sides for EV > threshold
          5. Size with fractional Kelly + caps
          6. Track cumulative exposure across opportunities

        Returns list of Opportunity objects (including skipped ones for audit).
        """
        if df.empty or "event_slug" not in df.columns:
            return []

        opportunities: list[Opportunity] = []
        cumulative_exposure = 0.0

        for event_slug, event_df in df.groupby("event_slug"):
            # Get event-level metadata from first row
            first = event_df.iloc[0]
            station_icao = first.get("station_icao", "")
            target_date = first.get("target_date", "")
            units = first.get("units", "F")

            # Get unique buckets in this event
            buckets = event_df.drop_duplicates(subset=["bucket_label"])[
                ["bucket_label", "bucket_low", "bucket_high",
                 "token_id", "condition_id",
                 "implied_prob", "best_bid", "best_ask",
                 "bid_depth", "ask_depth", "volume"]
            ].reset_index(drop=True)

            n_buckets = len(buckets)
            if n_buckets == 0:
                continue

            # Normalise implied probabilities (fix overround/underround)
            raw_probs = buckets["implied_prob"].tolist()
            total = sum(raw_probs)
            if total > 0:
                normalised = [p / total for p in raw_probs]
            else:
                normalised = [1.0 / n_buckets] * n_buckets

            for i, row in buckets.iterrows():
                bucket_label = row["bucket_label"]
                bucket_low = row["bucket_low"]
                bucket_high = row["bucket_high"]

                p_market = normalised[i]

                # Compute ask/bid prices (needed for both skip and trade paths)
                best_ask = row.get("best_ask")
                best_bid = row.get("best_bid")

                if best_ask is not None and best_ask > 0:
                    ask_yes = best_ask
                elif best_bid is not None and best_bid > 0:
                    ask_yes = best_bid + 0.01
                else:
                    ask_yes = p_market if p_market > 0 else 0.5

                if best_bid is not None and best_bid > 0:
                    ask_no = 1.0 - best_bid
                else:
                    ask_no = 1.0 - ask_yes

                # Stage 1: ensemble probability
                p_model, ens_size, model_temps = (
                    self.compute_ensemble_probability(
                        df=event_df,
                        bucket_label=bucket_label,
                        bucket_low=bucket_low,
                        bucket_high=bucket_high,
                        units=units,
                    )
                )

                # Skip if no ensemble data — can't compute p_model
                if ens_size == 0:
                    opportunities.append(Opportunity(
                        event_slug=event_slug,
                        station_icao=station_icao,
                        target_date=target_date,
                        bucket_label=bucket_label,
                        bucket_index=i,
                        side="SKIP",
                        units=units,
                        token_id=row.get("token_id", ""),
                        condition_id=row.get("condition_id", ""),
                        implied_prob=p_market,
                        best_ask=best_ask,
                        best_bid=best_bid,
                        ask_price=ask_yes,
                        p_model=0.0,
                        p_market=p_market,
                        divergence=0.0,
                        ensemble_size=0,
                        model_temps=[],
                        ev_per_share=0.0,
                        ev_pct=0.0,
                        f_star=0.0,
                        f_fractional=0.0,
                        kelly_dollars=0.0,
                        single_market_cap=0.0,
                        liquidity_cap=0.0,
                        final_bet_dollars=0.0,
                        shares=0.0,
                        max_loss=0.0,
                        max_profit=0.0,
                        binding_cap="none",
                        skipped=True,
                        skip_reason="No ensemble data (ens_size=0)",
                    ))
                    continue

                divergence = self.compute_divergence(p_model, p_market)

                side, ev, ask_price = self.find_edge(
                    p_model, ask_yes, ask_no
                )

                if side == "SKIP":
                    # Still record for audit, but skipped
                    opportunities.append(Opportunity(
                        event_slug=event_slug,
                        station_icao=station_icao,
                        target_date=target_date,
                        bucket_label=bucket_label,
                        bucket_index=i,
                        side="SKIP",
                        units=units,
                        token_id=row.get("token_id", ""),
                        condition_id=row.get("condition_id", ""),
                        implied_prob=p_market,
                        best_ask=best_ask,
                        best_bid=best_bid,
                        ask_price=ask_yes,
                        p_model=p_model,
                        p_market=p_market,
                        divergence=divergence,
                        ensemble_size=ens_size,
                        model_temps=model_temps,
                        ev_per_share=0.0,
                        ev_pct=0.0,
                        f_star=0.0,
                        f_fractional=0.0,
                        kelly_dollars=0.0,
                        single_market_cap=0.0,
                        liquidity_cap=0.0,
                        final_bet_dollars=0.0,
                        shares=0.0,
                        max_loss=0.0,
                        max_profit=0.0,
                        binding_cap="none",
                        skipped=True,
                        skip_reason="EV below threshold",
                    ))
                    continue

                # Stage 3: Kelly sizing
                ask_depth = row.get("ask_depth", 0.0) or 0.0
                sizing = self.size_position(
                    p_model=p_model if side == "YES" else 1.0 - p_model,
                    ask_price=ask_price,
                    ask_depth=ask_depth,
                    current_total_exposure=cumulative_exposure,
                )

                # Track cumulative exposure
                if not sizing["skipped"]:
                    cumulative_exposure += sizing["final_bet_dollars"]

                opp = Opportunity(
                    event_slug=event_slug,
                    station_icao=station_icao,
                    target_date=target_date,
                    bucket_label=bucket_label,
                    bucket_index=i,
                    side=side,
                    units=units,
                    token_id=row.get("token_id", ""),
                    condition_id=row.get("condition_id", ""),
                    implied_prob=p_market,
                    best_ask=best_ask,
                    best_bid=best_bid,
                    ask_price=ask_price,
                    p_model=p_model if side == "YES" else 1.0 - p_model,
                    p_market=p_market,
                    divergence=divergence,
                    ensemble_size=ens_size,
                    model_temps=model_temps,
                    ev_per_share=ev,
                    ev_pct=ev / ask_price if ask_price > 0 else 0.0,
                    f_star=sizing["f_star"],
                    f_fractional=sizing["f_fractional"],
                    kelly_dollars=sizing["kelly_dollars"],
                    single_market_cap=sizing["single_market_cap"],
                    liquidity_cap=sizing["liquidity_cap"],
                    final_bet_dollars=sizing["final_bet_dollars"],
                    shares=sizing["shares"],
                    max_loss=sizing["max_loss"],
                    max_profit=sizing["max_profit"],
                    binding_cap=sizing["binding_cap"],
                    skipped=sizing["skipped"],
                    skip_reason=sizing["skip_reason"],
                )
                opportunities.append(opp)

        return opportunities

    def evaluate_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Convenience: run evaluate() and return opportunities as DataFrame.
        Only includes non-skipped opportunities by default.
        """
        opps = self.evaluate(df)
        if not opps:
            return pd.DataFrame()
        return pd.DataFrame([o.to_dict() for o in opps])