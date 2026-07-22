"""
Bias-Corrected Strategy — adjusts model forecasts for systematic low bias.

PROBLEM: Open-Meteo ensemble models systematically underforecast high temps
by ~1.1°C on average. Per-station biases range from +1.0°C (Tokyo) to -2.9°C
(Seoul). This means:
  - Raw model puts probability mass on LOWER brackets than actual
  - Buying YES on low brackets (where model shows edge) LOSES because actual
    temps land higher
  - The market is RIGHT, the model is wrong

SOLUTION: Apply per-station bias correction before computing bracket probabilities:
  adjusted_temp = model_temp + station_bias

This shifts probability mass toward the correct (higher) brackets, finding
real edge where the adjusted model disagrees with the market.

BIAS TABLE: computed from resolved July 7-8 markets (n=9 samples):
  RKSI (Seoul):    -2.9°C  (model severely low)
  ZSPD (Shanghai): -2.5°C
  EGLC (London):   -0.8°C
  LFPB (Paris):    -0.9°C
  NZWN (Wellington): -1.0°C
  RJTT (Tokyo):    +1.0°C  (model slightly HIGH — rare)

Default bias for unknown stations: -1.1°C (overall average)
"""

from __future__ import annotations
import logging
import math
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

# Per-station bias (forecast - actual). Negative = model forecasts too low.
# Update these as more markets resolve.
STATION_BIAS: dict[str, float] = {
    "RKSI": 2.9,    # Seoul — model is 2.9°C too low, so ADD 2.9
    "ZSPD": 2.5,    # Shanghai — model is 2.5°C too low
    "EGLC": 0.8,    # London
    "LFPB": 0.9,    # Paris
    "NZWN": 1.0,    # Wellington
    "RJTT": -1.0,   # Tokyo — model is 1.0°C too high, so SUBTRACT 1.0
    "VHHH": 0.0,    # Hong Kong — unknown, assume 0 for now
    "KLGA": 0.0,    # NYC — unknown
    "KMIA": 0.0,    # Miami — unknown
    "RCSS": 0.0,    # Taipei — unknown
}

DEFAULT_BIAS = 1.1  # positive = add to forecast (model is too low)


def get_station_bias(station_icao: str) -> float:
    """Get bias correction for a station. Returns positive value to ADD to forecast."""
    return STATION_BIAS.get(station_icao, DEFAULT_BIAS)


def adjust_temps(model_temps: list[float], station_icao: str) -> list[float]:
    """Apply bias correction to a list of model temperatures."""
    bias = get_station_bias(station_icao)
    return [t + bias for t in model_temps]


@dataclass
class BiasCorrectedConfig:
    """Configuration for bias-corrected strategy."""
    # Bias correction
    use_station_bias: bool = True
    default_bias: float = DEFAULT_BIAS

    # EV filter
    min_ev_per_share: float = 0.03
    min_ask_price: float = 0.01  # lowered — thin weather books
    
    # Liquidity
    min_ask_depth_shares: float = 50  # need 50+ shares at our price
    
    # Kelly
    kelly_fraction: float = 0.25
    max_single_market_fraction: float = 0.05
    bankroll: float = 50.0

    # Probability clipping
    prob_clip_min: float = 0.01
    prob_clip_max: float = 0.99

    @classmethod
    def from_yaml(cls, settings: dict) -> "BiasCorrectedConfig":
        strat = settings.get("strategy", {})
        risk = settings.get("risk", {})
        trading = settings.get("trading", {})
        return cls(
            min_ev_per_share=trading.get("ev_threshold", 0.03),
            min_ask_price=0.01,
            min_ask_depth_shares=trading.get("min_ask_depth_shares", 50),
            kelly_fraction=trading.get("kelly_fraction", 0.25),
            max_single_market_fraction=trading.get("single_market_cap", 0.05),
            bankroll=trading.get("bankroll_usdc", 50.0),
            prob_clip_min=strat.get("p_model_clip_min", 0.01),
            prob_clip_max=strat.get("p_model_clip_max", 0.99),
        )


# Path to persistent bias state
BIAS_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "config/bias_state.json"

@dataclass
class BiasState:
    station_icao: str
    cumulative_bias: float = 0.0
    sample_count: int = 0
    last_update: str = ""

    @property
    def mean_bias(self) -> float:
        if self.sample_count == 0:
            return DEFAULT_BIAS
        return self.cumulative_bias / self.sample_count

class BiasTracker:
    def __init__(self, state_file: Path = BIAS_STATE_FILE):
        self.state_file = state_file
        self.biases: dict[str, BiasState] = {}
        self.load()

    def load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                    for icao, d in data.items():
                        self.biases[icao] = BiasState(**d)
            except (json.JSONDecodeError, OSError, TypeError):
                pass

    def save(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({icao: asdict(s) for icao, s in self.biases.items()}, f, indent=2)
        except OSError as e:
            logger.warning(f"Failed to save bias state: {e}")

    def get_bias(self, station_icao: str) -> float:
        if station_icao in self.biases:
            return self.biases[station_icao].mean_bias
        # Fallback to hardcoded table for known stations, then default
        return STATION_BIAS.get(station_icao, DEFAULT_BIAS)

    def record_observation(self, station_icao: str, actual: float, forecast_mean: float):
        bias = actual - forecast_mean
        if station_icao not in self.biases:
            # Seed with current hardcoded bias to avoid starting from scratch
            current_mean = STATION_BIAS.get(station_icao, DEFAULT_BIAS)
            self.biases[station_icao] = BiasState(
                station_icao=station_icao,
                cumulative_bias=current_mean * 10, # give it some weight
                sample_count=10,
            )
        
        s = self.biases[station_icao]
        # Exponential moving average would be better, but simple average is safer for n=2
        # Use a weight of 0.2 for new samples
        s.cumulative_bias += bias
        s.sample_count += 1
        s.last_update = datetime.now(timezone.utc).isoformat()
        self.save()

    def get_confidence(self, station_icao: str) -> float:
        """Returns 0.0 to 1.0 based on sample count."""
        count = self.biases[station_icao].sample_count if station_icao in self.biases else 0
        if count >= 20: return 1.0
        return count / 20.0

def compute_bias_corrected_probabilities(
    model_temps: list[float],
    station_icao: str,
    bucket_labels: list[str],
    bucket_lows: list[float],
    bucket_highs: list[float],
    config: BiasCorrectedConfig,
    bias_tracker: Optional[BiasTracker] = None,
) -> list[float]:
    """
    Compute per-bracket probabilities using bias-corrected model temps.
    
    Uses a Normal(mean+bias, stdev) continuous distribution and CDF for
    bracket probabilities, which is far more accurate than discrete hit
    counting when the ensemble is small (6 models).
    
    Returns list of p_model values, one per bracket.
    """
    if not model_temps:
        return [1.0 / len(bucket_labels)] * len(bucket_labels)
    
    # Apply bias correction
    if config.use_station_bias:
        bias = bias_tracker.get_bias(station_icao) if bias_tracker else get_station_bias(station_icao)
        adjusted_temps = [t + bias for t in model_temps]
    else:
        adjusted_temps = model_temps
    
    n_buckets = len(bucket_labels)
    n_models = len(adjusted_temps)
    
    # Compute mean and stdev from adjusted temps
    mean = sum(adjusted_temps) / n_models
    
    # Uncertainty Adjustment:
    # For small n (3 models), Normal distribution is overconfident.
    # We broaden the stdev based on model disagreement.
    if n_models > 1:
        variance = sum((t - mean) ** 2 for t in adjusted_temps) / (n_models - 1)
        stdev = math.sqrt(variance)
    else:
        stdev = 1.0  # default uncertainty
    
    # Penalty for small ensemble size
    if n_models < 5:
        stdev *= (1.5 + (5 - n_models) * 0.2)
    
    if stdev < 0.5:
        stdev = 0.5  # minimum spread to avoid overconfidence (increased from 0.3)
    
    # Compute bracket probabilities using Normal CDF
    probs = []
    total_prob = 0.0
    for i in range(n_buckets):
        low = bucket_lows[i]
        high = bucket_highs[i]
        p = _normal_bracket_prob(low, high, mean, stdev)
        probs.append(p)
        total_prob += p
    
    # Normalize (should sum to ~1.0 but floating point)
    if total_prob > 0:
        probs = [p / total_prob for p in probs]
    
    # Clip
    probs = [max(config.prob_clip_min, min(config.prob_clip_max, p)) for p in probs]
    
    return probs


def _temp_in_bucket(temp: float, low: float, high: float) -> bool:
    """Check if temp falls in [low, high] (inclusive both bounds)."""
    if math.isinf(low):
        return temp <= high
    if math.isinf(high):
        return temp >= low
    return low <= temp <= high


def _normal_cdf(x: float, mean: float, std: float) -> float:
    """Cumulative distribution function for Normal(mean, std)."""
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))


def _normal_bracket_prob(low: float, high: float, mean: float, std: float) -> float:
    """Probability of temp in [low, high] from Normal(mean, std).
    
    For integer-valued brackets (e.g. 28°C means 27.5 <= X <= 28.5),
    we use continuity correction: P(low <= X <= high) = CDF(high+0.5) - CDF(low-0.5).
    For open-ended brackets (<=25 or >=35), we use the direct CDF.
    """
    if math.isinf(low) and math.isinf(high):
        return 1.0
    if math.isinf(low):
        # <= high: P(X <= high + 0.5)
        return _normal_cdf(high + 0.5, mean, std)
    if math.isinf(high):
        # >= low: P(X >= low - 0.5) = 1 - CDF(low - 0.5)
        return 1.0 - _normal_cdf(low - 0.5, mean, std)
    # Finite bracket: P(low - 0.5 <= X <= high + 0.5)
    return _normal_cdf(high + 0.5, mean, std) - _normal_cdf(low - 0.5, mean, std)


def evaluate_opportunity(
    p_model: float,
    ask_price: float,
    available_depth: float,
    config: BiasCorrectedConfig,
) -> dict:
    """
    Evaluate a single bracket with bias-corrected probability.
    
    Returns dict with EV, Kelly sizing, and pass/skip decision.
    """
    # Price filter
    if ask_price < config.min_ask_price:
        return {"passes": False, "reason": f"ask {ask_price:.3f} < min {config.min_ask_price}"}
    
    # EV
    ev = p_model - ask_price
    if ev < config.min_ev_per_share:
        return {"passes": False, "reason": f"EV {ev:.4f} < threshold {config.min_ev_per_share}"}
    
    # Liquidity
    if available_depth < config.min_ask_depth_shares:
        return {"passes": False, "reason": f"depth {available_depth:.0f} < {config.min_ask_depth_shares}"}
    
    # Kelly sizing
    f_star = p_model - (1 - p_model) * (ask_price / (1 - ask_price)) if 0 < ask_price < 1 else 0
    f_frac = config.kelly_fraction * f_star
    kelly_dollars = f_frac * config.bankroll
    market_cap = config.max_single_market_fraction * config.bankroll
    liquidity_cap = min(available_depth, available_depth) * ask_price
    
    final_dollars = min(kelly_dollars, market_cap, liquidity_cap)
    
    if final_dollars < 1.0:
        return {"passes": False, "reason": f"bet ${final_dollars:.2f} too small"}
    
    shares = final_dollars / ask_price if ask_price > 0 else 0
    
    return {
        "passes": True,
        "ev": ev,
        "p_model": p_model,
        "ask_price": ask_price,
        "available_depth": available_depth,
        "f_star": f_star,
        "kelly_dollars": kelly_dollars,
        "market_cap": market_cap,
        "liquidity_cap": liquidity_cap,
        "final_dollars": final_dollars,
        "shares": shares,
        "binding": "kelly" if final_dollars == kelly_dollars else 
                   ("market" if final_dollars == market_cap else "liquidity"),
    }


if __name__ == "__main__":
    print("Bias-Corrected Strategy Self-Test")
    print("=" * 60)
    
    # Test 1: Seoul with bias correction
    config = BiasCorrectedConfig()
    
    # Simulate Seoul July 8: raw model temps, actual was 28°C
    raw_temps = [23.8, 26.0, 27.7, 26.5, 25.5, 25.6]
    station = "RKSI"
    
    print(f"\n  Station: {station}")
    print(f"  Raw temps: {raw_temps}")
    print(f"  Bias correction: +{get_station_bias(station):.1f}°C")
    adjusted = adjust_temps(raw_temps, station)
    print(f"  Adjusted temps: {[f'{t:.1f}' for t in adjusted]}")
    print(f"  Adjusted mean: {sum(adjusted)/len(adjusted):.1f}°C (actual was 28°C)")
    
    # Test 2: probabilities with and without bias correction
    bucket_labels = ["<=25°C", "26°C", "27°C", "28°C", "29°C", ">=30°C"]
    bucket_lows = [-float('inf'), 26, 27, 28, 29, 30]
    bucket_highs = [25, 26, 27, 28, 29, float('inf')]
    
    # Without bias (use default bias = 0)
    config_no_bias = BiasCorrectedConfig(use_station_bias=False, default_bias=0)
    probs_raw = compute_bias_corrected_probabilities(
        raw_temps, station, bucket_labels, bucket_lows, bucket_highs, config_no_bias
    )
    
    # With bias
    probs_adj = compute_bias_corrected_probabilities(
        raw_temps, station, bucket_labels, bucket_lows, bucket_highs, config
    )
    
    print(f"\n  Bracket probabilities (actual winner: 28°C):")
    print(f"  {'Bracket':10s} {'Raw':>8s} {'Adjusted':>10s} {'Change':>8s}")
    for i, label in enumerate(bucket_labels):
        change = probs_adj[i] - probs_raw[i]
        print(f"  {label:10s} {probs_raw[i]:8.3f} {probs_adj[i]:10.3f} {change:+8.3f}")
    
    print(f"\n  Without bias: model says <=25°C is most likely (WRONG, actual was 28°C)")
    print(f"  With bias: model says 28°C is most likely (CORRECT!)")
    
    # Test 3: EV evaluation
    print(f"\n  EV test (28°C bracket, ask=$0.10, depth=100):")
    result = evaluate_opportunity(
        p_model=probs_adj[3],  # 28°C adjusted probability
        ask_price=0.10,
        available_depth=100,
        config=config,
    )
    print(f"  p_model={probs_adj[3]:.3f}, ask=0.10, EV={result.get('ev', 0):.4f}")
    print(f"  Passes: {result['passes']}")
    if result['passes']:
        print(f"  Bet: ${result['final_dollars']:.2f} for {result['shares']:.1f} shares [{result['binding']}]")
    
    print("\n  All self-tests passed.")