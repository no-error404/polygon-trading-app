"""
Kelly — fractional Kelly Criterion position sizing with hard caps.

Three caps applied, smallest wins:
  1. Kelly fraction: f = kelly_fraction * f*
  2. Single-market cap: max single_market_cap of bankroll
  3. Liquidity cap: shares limited to top-3 ask depth from orderbook

Binary $0/$1 Kelly formula:
  f* = p - (1-p) * (price / (1 - price))

where p = p_model, price = ask price (what we pay)

We use QUARTER-Kelly (kelly_fraction=0.25) by default.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class KellyResult:
    """Position sizing result."""
    bracket_label: str
    p_model: float
    price: float
    f_star: float          # full Kelly fraction
    f_fractional: float    # after fractional Kelly (e.g. quarter-Kelly)
    kelly_dollars: float   # fractional Kelly in dollars
    market_cap_dollars: float  # single-market cap in dollars
    liquidity_cap_dollars: float  # liquidity cap in dollars
    final_dollars: float   # final position size (smallest of the 3 caps)
    final_shares: float    # final position in shares
    binding_constraint: str  # "kelly", "market_cap", or "liquidity"
    max_loss: float        # max possible loss in dollars
    max_profit: float      # max possible profit in dollars


def kelly_fraction(
    p_model: float,
    price: float,
) -> float:
    """
    Compute full Kelly fraction for a binary $0/$1 bet.

    f* = p - (1-p) * (price / (1 - price))

    Returns 0.0 if no edge (price >= 1 or price <= 0, or f* <= 0).
    """
    if price <= 0 or price >= 1:
        return 0.0

    f_star = p_model - (1 - p_model) * (price / (1 - price))
    return max(0.0, f_star)


def size_position(
    p_model: float,
    price: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    single_market_cap: float = 0.05,
    ask_depth_top3: float = 0.0,
    ev_per_share: float = 0.0,
    bracket_label: str = "",
) -> KellyResult:
    """
    Size a position using fractional Kelly with hard caps.

    Args:
        p_model: model probability (clipped to [0.01, 0.99])
        price: ask price we'd pay
        bankroll: current bankroll in USDC
        kelly_fraction: fraction of full Kelly (default 0.25 = quarter-Kelly)
        single_market_cap: max fraction of bankroll per market (default 5%)
        ask_depth_top3: total size of top 3 ask levels (for liquidity cap)
        ev_per_share: EV per share (for logging only)
        bracket_label: bracket label (for logging)

    Returns:
        KellyResult with final_dollars, final_shares, and binding constraint
    """
    # Compute full Kelly
    f_star = kelly_fraction_calc(p_model, price)

    # Apply fractional Kelly
    f_frac = kelly_fraction * f_star
    kelly_dollars = f_frac * bankroll

    # Single-market cap
    market_cap_dollars = single_market_cap * bankroll

    # Liquidity cap: shares limited to top-3 ask depth
    # Convert shares to dollars: dollars = shares * price
    liquidity_cap_dollars = ask_depth_top3 * price if ask_depth_top3 > 0 else float('inf')

    # Final = smallest of the three
    if kelly_dollars <= 0:
        return KellyResult(
            bracket_label=bracket_label, p_model=p_model, price=price,
            f_star=f_star, f_fractional=f_frac,
            kelly_dollars=0, market_cap_dollars=market_cap_dollars,
            liquidity_cap_dollars=liquidity_cap_dollars,
            final_dollars=0, final_shares=0,
            binding_constraint="no_edge", max_loss=0, max_profit=0,
        )

    final_dollars = min(kelly_dollars, market_cap_dollars, liquidity_cap_dollars)

    # Determine binding constraint
    if final_dollars == kelly_dollars:
        binding = "kelly"
    elif final_dollars == market_cap_dollars:
        binding = "market_cap"
    else:
        binding = "liquidity"

    # Compute shares and P&L
    final_shares = final_dollars / price if price > 0 else 0
    max_loss = final_dollars  # we lose the full cost if NO wins
    max_profit = final_shares * (1.0 - price)  # profit = shares * (1 - price)

    return KellyResult(
        bracket_label=bracket_label,
        p_model=p_model,
        price=price,
        f_star=f_star,
        f_fractional=f_frac,
        kelly_dollars=kelly_dollars,
        market_cap_dollars=market_cap_dollars,
        liquidity_cap_dollars=liquidity_cap_dollars,
        final_dollars=final_dollars,
        final_shares=final_shares,
        binding_constraint=binding,
        max_loss=max_loss,
        max_profit=max_profit,
    )


def kelly_fraction_calc(p_model: float, price: float) -> float:
    """Alias for kelly_fraction to avoid name collision with parameter."""
    if price <= 0 or price >= 1:
        return 0.0
    f_star = p_model - (1 - p_model) * (price / (1 - price))
    return max(0.0, f_star)


if __name__ == "__main__":
    print("Kelly Self-Test")
    print("=" * 60)

    # Phase 1 worked example:
    # p_model=0.75, price=0.30, bankroll=$1000, quarter-Kelly, 5% cap
    result = size_position(
        p_model=0.75,
        price=0.30,
        bankroll=1000,
        kelly_fraction=0.25,
        single_market_cap=0.05,
        ask_depth_top3=200,  # 200 shares available
        bracket_label="86-87°F",
    )

    print(f"\n  Worked example: p=0.75, price=0.30, bankroll=$1000")
    print(f"  f* (full Kelly): {result.f_star:.4f} (expected: 0.6429)")
    print(f"  f (quarter):    {result.f_fractional:.4f} (expected: 0.1607)")
    print(f"  Kelly $:        {result.kelly_dollars:.2f} (expected: ~$160.71)")
    print(f"  Market cap $:   {result.market_cap_dollars:.2f} (expected: $50.00)")
    print(f"  Liquidity cap $: {result.liquidity_cap_dollars:.2f} (expected: $60.00)")
    print(f"  Final $:        {result.final_dollars:.2f} (expected: $50.00 — market cap binds)")
    print(f"  Final shares:   {result.final_shares:.2f} (expected: ~166.67)")
    print(f"  Binding:        {result.binding_constraint} (expected: market_cap)")
    print(f"  Max loss:       ${result.max_loss:.2f}")
    print(f"  Max profit:     ${result.max_profit:.2f}")

    assert abs(result.f_star - 0.6429) < 0.01, "f* wrong"
    assert abs(result.kelly_dollars - 160.71) < 1, "kelly_dollars wrong"
    assert abs(result.market_cap_dollars - 50) < 0.01, "market cap wrong"
    assert abs(result.final_dollars - 50) < 0.01, "final should be market cap"
    assert result.binding_constraint == "market_cap"

    # Test: no edge (f* = 0)
    result2 = size_position(p_model=0.50, price=0.50, bankroll=1000)
    print(f"\n  No edge: f*={result2.f_star:.4f} final=${result2.final_dollars:.2f}")
    assert result2.final_dollars == 0
    assert result2.binding_constraint == "no_edge"

    # Test: liquidity binds
    result3 = size_position(
        p_model=0.75, price=0.30, bankroll=10000,
        kelly_fraction=0.25, single_market_cap=0.05,
        ask_depth_top3=10,  # only 10 shares available
    )
    print(f"\n  Liquidity binds: final=${result3.final_dollars:.2f} shares={result3.final_shares:.2f}")
    print(f"  Binding: {result3.binding_constraint}")
    assert result3.binding_constraint == "liquidity"
    assert abs(result3.final_dollars - 3.0) < 0.01  # 10 shares * 0.30 price

    print("\n  All self-tests passed.")