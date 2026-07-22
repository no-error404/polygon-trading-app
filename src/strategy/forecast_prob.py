"""
Forecast Probability — convert ensemble max temps into per-bracket probabilities.

Takes an EnsembleResult (multi-model max temps) and a WeatherMarket
(temperature brackets) and computes p_model for each bracket.

Key logic:
  - Each model contributes one max temp observation
  - p_model = count(models landing in bracket) / total_models
  - Small ensemble (<30) → Bayesian shrinkage with Laplace prior
  - Clip p_model to [0.01, 0.99] to prevent Kelly div-by-zero

SHRINKAGE DESIGN (fixed 2026-07-08):
  Old approach blended toward uniform (1/N), which inflated zero-hit
  tail brackets to ~7% and created fake edge at sub-penny ask prices.
  The bot bought tail brackets its own model said were impossible.

  New approach uses a Laplace (additive) prior: add pseudo-counts
  proportional to (1-shrink) to each bracket, not flat probability mass.
  This keeps zero-hit brackets near zero while regularising the
  distribution for small ensembles. The total pseudo-count alpha is
  scaled by ensemble size so larger ensembles need less regularisation.
"""

from dataclasses import dataclass, field
from typing import Optional
import math

from src.data.weather_client import EnsembleResult
from src.strategy.market_mapper import WeatherMarket, Bracket


@dataclass
class BracketProbability:
    """Model probability for a single bracket."""
    bracket: Bracket
    p_model: float          # model probability (after shrinkage + clipping)
    p_raw: float            # raw model probability (before shrinkage)
    model_hits: int = 0     # how many models landed in this bracket
    model_temps: list = field(default_factory=list)  # which model temps hit

    @property
    def edge(self) -> float:
        """Edge = p_model - p_market (implied prob from price)."""
        return self.p_model - self.bracket.yes_price


def compute_bracket_probabilities(
    ensemble: EnsembleResult,
    market: WeatherMarket,
    min_ensemble: int = 30,
    clip_min: float = 0.01,
    clip_max: float = 0.99,
) -> list[BracketProbability]:
    """
    Compute per-bracket model probabilities from ensemble forecasts.

    Args:
        ensemble: multi-model forecast results
        market: weather market with brackets
        min_ensemble: minimum model count before shrinkage kicks in
        clip_min, clip_max: clipping bounds for p_model

    Returns:
        List of BracketProbability, one per bracket
    """
    # Use min temps for "low" metric markets, max temps for "high"
    temps_all = ensemble.min_temps_all if market.metric == "low" else ensemble.max_temps_all
    n_models = len(temps_all)
    n_brackets = len(market.brackets)

    if n_brackets == 0:
        return []

    # Count hits per bracket
    hits_per_bracket: list[int] = []
    hits_temps: list[list[float]] = []
    for bracket in market.brackets:
        hits = [t for t in temps_all if bracket.contains(t)]
        hits_per_bracket.append(len(hits))
        hits_temps.append(hits)

    # --- Bayesian shrinkage with Laplace prior ---
    # Instead of blending toward uniform (which inflates zero-hit tails),
    # add pseudo-counts alpha to each bracket's hit count, then normalise.
    #
    # alpha = (1 - shrink) * n_models / n_brackets
    #   - shrink = n_models / min_ensemble (0..1): how much we trust the raw
    #   - When shrink=1 (large ensemble): alpha=0, pure raw frequencies
    #   - When shrink=0.2 (6 models, 30 threshold): alpha = 0.8*6/11 ≈ 0.44
    #     A zero-hit bracket gets p = 0.44 / (6 + 0.44*11) = 0.44/10.84 ≈ 4.1%
    #     (vs 7.27% with old uniform shrinkage — and it scales with n_models,
    #      so 0 hits out of 6 is treated very differently from 0 hits out of 30)
    #
    # For zero-hit brackets: p_model = alpha / (n_models + alpha * n_brackets)
    # For k-hit brackets:    p_model = (k + alpha) / (n_models + alpha * n_brackets)
    # This is a proper Dirichlet-multinomial posterior with symmetric prior.
    if n_models > 0:
        if n_models < min_ensemble:
            shrink = n_models / min_ensemble
        else:
            shrink = 1.0
        alpha = (1.0 - shrink) * n_models / n_brackets
        denom = n_models + alpha * n_brackets
    else:
        alpha = 0.0
        denom = 1.0

    results = []
    for i, bracket in enumerate(market.brackets):
        k = hits_per_bracket[i]
        p_raw = k / n_models if n_models > 0 else 0.0
        p_model = (k + alpha) / denom if denom > 0 else 0.0

        # Clip to prevent Kelly div-by-zero
        p_model = max(clip_min, min(clip_max, p_model))

        results.append(BracketProbability(
            bracket=bracket,
            p_model=p_model,
            p_raw=p_raw,
            model_hits=k,
            model_temps=hits_temps[i],
        ))

    # Normalise p_model so they sum to 1.0 (after clipping)
    total = sum(r.p_model for r in results)
    if total > 0:
        for r in results:
            r.p_model = r.p_model / total

    return results


def best_opportunity(probs: list[BracketProbability]) -> Optional[BracketProbability]:
    """Return the bracket with the largest positive edge (p_model - p_market)."""
    positive = [p for p in probs if p.edge > 0]
    if not positive:
        return None
    return max(positive, key=lambda p: p.edge)


if __name__ == "__main__":
    print("Forecast Probability Self-Test")
    print("=" * 60)

    # Create a synthetic market and ensemble
    brackets = [
        Bracket(label="<=77°F", low=float('-inf'), high=77, market_index=0, yes_price=0.05),
        Bracket(label="78-79°F", low=78, high=79, market_index=1, yes_price=0.10),
        Bracket(label="80-81°F", low=80, high=81, market_index=2, yes_price=0.20),
        Bracket(label="82-83°F", low=82, high=83, market_index=3, yes_price=0.25),
        Bracket(label="84-85°F", low=84, high=85, market_index=4, yes_price=0.15),
        Bracket(label="86-87°F", low=86, high=87, market_index=5, yes_price=0.15),
        Bracket(label=">=88°F", low=88, high=float('inf'), market_index=6, yes_price=0.10),
    ]
    market = WeatherMarket(
        title="Test NYC temp", slug="test", station_icao="KLGA",
        units="F", target_date="2026-07-16", brackets=brackets,
    )
    market.normalise()

    # Synthetic ensemble: 3 models with max temps 83, 86, 85
    ensemble = EnsembleResult(station_icao="KLGA", target_date="2026-07-16", units="F")
    ensemble.max_temps_all = [83, 86, 85]

    probs = compute_bracket_probabilities(ensemble, market)
    print(f"\n  Models: {len(ensemble.max_temps_all)} temps: {ensemble.max_temps_all}")
    print(f"  Brackets: {len(probs)}")
    print()
    for p in probs:
        print(f"    {p.bracket.label:>12s}: p_model={p.p_model:.4f} p_market={p.bracket.yes_price:.4f} edge={p.edge:+.4f} hits={p.model_hits}")

    print()
    best = best_opportunity(probs)
    if best:
        print(f"  Best opportunity: {best.bracket.label} (edge={best.edge:+.4f})")
    else:
        print("  No positive edge found")

    # Verify probabilities sum to 1.0
    total = sum(p.p_model for p in probs)
    print(f"\n  Sum of p_model: {total:.6f} (should be 1.0)")
    print(f"  All clipped to [{min(p.p_model for p in probs):.4f}, {max(p.p_model for p in probs):.4f}]")

    print("\n  Self-test complete.")