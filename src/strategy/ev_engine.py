"""
EV Engine — Expected Value calculations for weather bracket markets.

EV per $1-share of YES:
  EV = p_model * (1.00 - price) - (1 - p_model) * price

Hard filters:
  - Skip if EV < ev_threshold (noise filter, default $0.03/share)
  - Skip if EV <= 0 (no edge)
  - Always compute EV against the ASK price (not mid) for buys
"""

from dataclasses import dataclass
from typing import Optional

from src.strategy.forecast_prob import BracketProbability


@dataclass
class EVResult:
    """EV calculation result for a single bracket."""
    bracket_label: str
    side: str               # "BUY" or "SELL"
    p_model: float
    price: float            # the price we'd pay (ask for buys, bid for sells)
    ev_per_share: float     # expected value per $1 share
    ev_pct: float           # EV as percentage of cost
    passes_filter: bool     # True if EV > threshold and EV > 0
    token_id: str           # CLOB token ID to trade
    p_market: float         # market's implied probability


MIN_ASK_PRICE = 0.02  # don't buy anything below 2¢ — market correctly
                       # prices these as near-impossible; sub-penny asks
                       # create fake edge when p_model is inflated by shrinkage


def compute_ev(
    prob: BracketProbability,
    ask_price: float = 0.0,
    ev_threshold: float = 0.03,
    min_ask_price: float = MIN_ASK_PRICE,
) -> EVResult:
    """
    Compute Expected Value for buying YES on a bracket.

    Args:
        prob: BracketProbability from forecast_prob
        ask_price: the ask price we'd actually pay (from orderbook)
                   if 0.0, uses the market's yes_price as fallback
        ev_threshold: minimum EV to pass filter ($0.03 default)
        min_ask_price: don't buy shares priced below this (default 2¢).
                       Sub-penny tail brackets have no real edge — the
                       market is right and any model probability is
                       shrinkage noise, not genuine signal.

    Returns:
        EVResult with ev_per_share and pass/fail status
    """
    p = prob.p_model
    # Use ask price if provided, otherwise fall back to yes_price
    price = ask_price if ask_price > 0 else prob.bracket.yes_price
    token_id = prob.bracket.yes_token

    # EV = p * (1 - price) - (1-p) * price
    ev = p * (1.0 - price) - (1.0 - p) * price
    ev_pct = ev / price if price > 0 else 0.0

    passes = ev > ev_threshold and ev > 0 and price >= min_ask_price

    return EVResult(
        bracket_label=prob.bracket.label,
        side="BUY",
        p_model=p,
        price=price,
        ev_per_share=ev,
        ev_pct=ev_pct,
        passes_filter=passes,
        token_id=token_id,
        p_market=prob.bracket.yes_price,
    )


def filter_opportunities(
    probs: list[BracketProbability],
    ask_prices: dict = None,  # {bracket_label: ask_price}
    ev_threshold: float = 0.03,
    resolved_threshold: float = 0.95,
) -> list[EVResult]:
    """
    Compute EV for all brackets and return those that pass the filter.

    Args:
        probs: list of BracketProbability
        ask_prices: dict mapping bracket labels to ask prices from orderbook
        ev_threshold: minimum EV to pass ($0.03 default)
        resolved_threshold: if any single bracket's ask price exceeds this,
            the market is considered already resolved (or near-resolved) and
            ALL opportunities are suppressed. A 96¢ bracket means the outcome
            is known — buying any other bracket at 1¢ is not "edge", it's a
            guaranteed loss against an informed market.

    Returns:
        List of EVResults that pass the filter, sorted by EV descending
    """
    if ask_prices is None:
        ask_prices = {}

    # Resolved-market guard: if any bracket is trading above resolved_threshold,
    # the market knows the answer. Skip everything.
    max_ask = max((ask_prices.get(p.bracket.label, 0.0) for p in probs), default=0.0)
    if max_ask >= resolved_threshold:
        return []

    results = []
    for prob in probs:
        ask = ask_prices.get(prob.bracket.label, 0.0)
        ev = compute_ev(prob, ask, ev_threshold)
        if ev.passes_filter:
            results.append(ev)

    # Sort by EV descending
    results.sort(key=lambda x: x.ev_per_share, reverse=True)
    return results


if __name__ == "__main__":
    print("EV Engine Self-Test")
    print("=" * 60)

    # Test with the Phase 1 worked example:
    # p_model=0.75, price=0.30 → EV = 0.75*0.70 - 0.25*0.30 = 0.525 - 0.075 = 0.45
    from src.strategy.market_mapper import Bracket
    from src.strategy.forecast_prob import BracketProbability

    bracket = Bracket(
        label="86-87°F", low=86, high=87, market_index=5,
        yes_token="1234567890", yes_price=0.30,
    )
    prob = BracketProbability(
        bracket=bracket, p_model=0.75, p_raw=0.75, model_hits=90, model_temps=[86.5]*90,
    )

    ev = compute_ev(prob, ask_price=0.30, ev_threshold=0.03)
    print(f"\n  Worked example: p_model=0.75, price=0.30")
    print(f"  EV per share: {ev.ev_per_share:.4f} (expected: 0.4500)")
    print(f"  EV %: {ev.ev_pct:.4f} (expected: 1.5000)")
    print(f"  Passes filter: {ev.passes_filter} (expected: True)")
    assert abs(ev.ev_per_share - 0.45) < 0.001, "EV calculation wrong"
    assert ev.passes_filter == True, "Should pass filter"

    # Test: no edge (p_model == p_market)
    prob2 = BracketProbability(
        bracket=Bracket(label="test", low=0, high=100, yes_price=0.50),
        p_model=0.50, p_raw=0.50,
    )
    ev2 = compute_ev(prob2, ask_price=0.50, ev_threshold=0.03)
    print(f"\n  No edge: p_model=0.50, price=0.50")
    print(f"  EV: {ev2.ev_per_share:.4f} (expected: 0.0000)")
    print(f"  Passes: {ev2.passes_filter} (expected: False)")
    assert abs(ev2.ev_per_share) < 0.001, "No-edge EV should be 0"
    assert ev2.passes_filter == False

    # Test: negative edge
    prob3 = BracketProbability(
        bracket=Bracket(label="test", low=0, high=100, yes_price=0.80),
        p_model=0.20, p_raw=0.20,
    )
    ev3 = compute_ev(prob3, ask_price=0.80, ev_threshold=0.03)
    print(f"\n  Negative edge: p_model=0.20, price=0.80")
    print(f"  EV: {ev3.ev_per_share:.4f}")
    print(f"  Passes: {ev3.passes_filter} (expected: False)")
    assert ev3.passes_filter == False

    # Test: noise threshold
    prob4 = BracketProbability(
        bracket=Bracket(label="test", low=0, high=100, yes_price=0.45),
        p_model=0.52, p_raw=0.52,
    )
    ev4 = compute_ev(prob4, ask_price=0.45, ev_threshold=0.03)
    print(f"\n  Noise threshold: p_model=0.52, price=0.45")
    print(f"  EV: {ev4.ev_per_share:.4f}")
    print(f"  Passes: {ev4.passes_filter} (should be False — EV < 0.03)")
    print(f"  EV > 0 but < threshold: {0 < ev4.ev_per_share < 0.03}")

    print("\n  All self-tests passed.")