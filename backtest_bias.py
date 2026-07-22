#!/usr/bin/env python3
"""
Backtest: Bias-Corrected Weather Trading Strategy on Resolved Markets

For each resolved Polymarket temperature market:
  1. Fetch multi-model ensemble forecast from Open-Meteo
  2. Compute bias-corrected bracket probabilities
  3. Parse temperature brackets from the Gamma event's market questions
  4. Find which bracket the actual temperature fell in
  5. Rank brackets by model probability — check top-1 and top-2 accuracy
  6. Report model probability for the actual (winning) bracket
  7. Simulate trades: if p_model > market_price + 0.03, count as "buy", check win/loss
"""

import sys
import json
import math
import re
import yaml

sys.path.insert(0, '/home/rory/Documents/polymarket-weather-trader')

from src.strategy.bias_corrected import (
    BiasCorrectedConfig,
    compute_bias_corrected_probabilities,
    evaluate_opportunity,
    get_station_bias,
    adjust_temps,
)
from src.data.weather_client import fetch_station_forecast
from src.data.station_lookup import StationLookup
from src.data.gamma_client import get_event

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

PROJECT_DIR = '/home/rory/Documents/polymarket-weather-trader'
lookup = StationLookup(f'{PROJECT_DIR}/config/stations.yaml')
config = BiasCorrectedConfig.from_yaml(
    yaml.safe_load(open(f'{PROJECT_DIR}/config/settings.yaml'))
)

# (station_icao, target_date, actual_temp, temp_type, event_slug)
# temp_type: 'high' = highest temp of the day
resolutions = [
    ('RKSI', '2026-07-07', 30, 'high', 'highest-temperature-in-seoul-on-july-7-2026'),
    ('RJTT', '2026-07-07', 24, 'high', 'highest-temperature-in-tokyo-on-july-7-2026'),
    ('EGLC', '2026-07-07', 32, 'high', 'highest-temperature-in-london-on-july-7-2026'),
    ('RKSI', '2026-07-08', 28, 'high', 'highest-temperature-in-seoul-on-july-8-2026'),
    ('RJTT', '2026-07-08', 28, 'high', 'highest-temperature-in-tokyo-on-july-8-2026'),
    ('EGLC', '2026-07-08', 33, 'high', 'highest-temperature-in-london-on-july-8-2026'),
    ('ZSPD', '2026-07-08', 36, 'high', 'highest-temperature-in-shanghai-on-july-8-2026'),
    ('LFPB', '2026-07-08', 34, 'high', 'highest-temperature-in-paris-on-july-8-2026'),
    ('NZWN', '2026-07-08', 13, 'high', 'highest-temperature-in-wellington-on-july-8-2026'),
]


# ---------------------------------------------------------------------------
# Bracket parsing
# ---------------------------------------------------------------------------

BRACKET_RE = re.compile(
    r'(?:be\s+)?(?:between\s+)?(\d+)(?:[–-](\d+))?\s*°C(?:\s+or\s+(higher|lower))?'
)


def parse_brackets(markets):
    """
    Parse temperature brackets from a list of MarketInfo objects.

    Returns (labels, lows, highs) lists. Open-ended brackets use ±inf.
    Returns None if parsing fails for any market.
    """
    labels = []
    lows = []
    highs = []

    for m in markets:
        q = m.question
        match = BRACKET_RE.search(q)
        if not match:
            return None

        low_str = match.group(1)
        high_str = match.group(2)
        suffix = match.group(3)  # "higher" or "lower" or None

        low = int(low_str)
        high = int(high_str) if high_str else low

        if suffix == "higher":
            # "35°C or higher" → [low, +inf)
            label = f"{low}°C+"
            lows.append(float(low))
            highs.append(float('inf'))
        elif suffix == "lower":
            # "25°C or lower" → (-inf, high]
            label = f"≤{low}°C"
            lows.append(float('-inf'))
            highs.append(float(low))
        elif high_str:
            # Range "26-30°C"
            label = f"{low}-{high}°C"
            lows.append(float(low))
            highs.append(float(high))
        else:
            # Single value "28°C" — treat as [28, 28]
            label = f"{low}°C"
            lows.append(float(low))
            highs.append(float(low))

        labels.append(label)

    return labels, lows, highs


def find_winning_bracket(actual_temp, lows, highs):
    """Find which bracket index the actual temperature falls into."""
    for i, (low, high) in enumerate(zip(lows, highs)):
        if math.isinf(low) and math.isinf(high):
            return i
        if math.isinf(low):
            if actual_temp <= high:
                return i
        elif math.isinf(high):
            if actual_temp >= low:
                return i
        else:
            if low <= actual_temp <= high:
                return i
    return None


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

print("=" * 100)
print("BIAS-CORRECTED WEATHER STRATEGY — BACKTEST ON RESOLVED MARKETS")
print("=" * 100)
print()

results = []

for station_icao, target_date, actual_temp, temp_type, slug in resolutions:
    print(f"\n{'─' * 90}")
    print(f"  Station: {station_icao}  Date: {target_date}  Actual: {actual_temp}°C  Slug: {slug}")
    print(f"{'─' * 90}")

    # 1. Get station object
    try:
        station = lookup.get_station(station_icao)
    except KeyError as e:
        print(f"  [ERROR] Station not found: {e}")
        results.append({'station': station_icao, 'date': target_date, 'error': str(e)})
        continue

    # 2. Fetch ensemble forecast
    print(f"  Fetching ensemble forecast ({station.name})...")
    try:
        ensemble = fetch_station_forecast(station, target_date)
    except Exception as e:
        print(f"  [ERROR] Forecast fetch failed: {e}")
        results.append({'station': station_icao, 'date': target_date, 'error': str(e)})
        continue

    model_temps = ensemble.max_temps_all  # these are in station.units (C for all our stations)
    print(f"  Models returned: {len(ensemble.models)}")
    for model_name, forecast in ensemble.models.items():
        print(f"    {model_name:20s} max={forecast.max_temp:.1f}°C")
    print(f"  Raw max temps: {[f'{t:.1f}' for t in model_temps]}")

    if not model_temps:
        print(f"  [ERROR] No model temps returned")
        results.append({'station': station_icao, 'date': target_date, 'error': 'no model temps'})
        continue

    # Bias info
    bias = get_station_bias(station_icao)
    adjusted_temps = adjust_temps(model_temps, station_icao)
    raw_mean = sum(model_temps) / len(model_temps)
    adj_mean = sum(adjusted_temps) / len(adjusted_temps)
    print(f"  Bias correction: +{bias:.1f}°C  raw_mean={raw_mean:.1f}°C  adjusted_mean={adj_mean:.1f}°C  (actual={actual_temp}°C)")

    # 3. Fetch event from Gamma
    print(f"  Fetching Gamma event: {slug}...")
    try:
        event = get_event(slug)
    except Exception as e:
        print(f"  [ERROR] Gamma fetch failed: {e}")
        results.append({'station': station_icao, 'date': target_date, 'error': str(e)})
        continue

    if not event:
        print(f"  [ERROR] Event not found")
        results.append({'station': station_icao, 'date': target_date, 'error': 'event not found'})
        continue

    print(f"  Event: '{event.title}'  closed={event.closed}  markets={len(event.markets)}")

    if not event.markets:
        print(f"  [ERROR] No markets in event")
        results.append({'station': station_icao, 'date': target_date, 'error': 'no markets'})
        continue

    # 4. Parse brackets
    parsed = parse_brackets(event.markets)
    if not parsed:
        print(f"  [ERROR] Failed to parse brackets")
        for m in event.markets:
            print(f"    Q: {m.question}")
        results.append({'station': station_icao, 'date': target_date, 'error': 'bracket parse failed'})
        continue

    labels, lows, highs = parsed
    print(f"  Parsed {len(labels)} brackets:")
    for i, (label, low, high) in enumerate(zip(labels, lows, highs)):
        m = event.markets[i]
        print(f"    [{i}] {label:12s}  low={low:>6}  high={high:>6}  yes_price={m.yes_price:.2f}")

    # 5. Compute bias-corrected probabilities
    probs = compute_bias_corrected_probabilities(
        model_temps, station_icao, labels, lows, highs, config
    )

    print(f"\n  Model probabilities (bias-corrected):")
    for i, (label, p) in enumerate(zip(labels, probs)):
        print(f"    [{i}] {label:12s}  p_model={p:.4f}  market={event.markets[i].yes_price:.2f}")

    # 6. Find winning bracket
    winner_idx = find_winning_bracket(actual_temp, lows, highs)
    if winner_idx is None:
        print(f"  [WARN] Actual temp {actual_temp}°C not in any bracket!")
        # Try to find closest
        results.append({'station': station_icao, 'date': target_date, 'error': 'no bracket for actual'})
        continue

    winner_label = labels[winner_idx]
    winner_p_model = probs[winner_idx]
    winner_market_price = event.markets[winner_idx].yes_price

    # 7. Rank brackets by model probability
    ranked = sorted(range(len(labels)), key=lambda i: probs[i], reverse=True)
    top1_idx = ranked[0]
    top2_idx = ranked[1] if len(ranked) > 1 else ranked[0]
    top1_match = top1_idx == winner_idx
    top2_match = winner_idx in ranked[:2]

    print(f"\n  RESULTS:")
    print(f"    Actual temp:     {actual_temp}°C")
    print(f"    Winning bracket: [{winner_idx}] {winner_label}")
    print(f"    Model top-1:     [{top1_idx}] {labels[top1_idx]}  p={probs[top1_idx]:.4f}  {'✓ MATCH' if top1_match else '✗ MISS'}")
    print(f"    Model top-2:     [{top2_idx}] {labels[top2_idx]}  p={probs[top2_idx]:.4f}  {'✓ in top-2' if top2_match else '✗ not in top-2'}")
    print(f"    P(actual):       {winner_p_model:.4f}")
    print(f"    Market price:    {winner_market_price:.4f}  (resolved: {'YES' if winner_market_price > 0.5 else 'NO'})")

    # 8. Simulate trades
    # For each bracket: if p_model > market_price + 0.03, "buy YES"
    # Since markets are resolved, market_price is 1.0 (winner) or ~0.0 (loser)
    # We use the resolution outcome for win/loss
    trades = []
    for i, (label, p_model, m) in enumerate(zip(labels, probs, event.markets)):
        market_price = m.yes_price
        is_winner = i == winner_idx

        # Buy signal: model edge over market price
        edge = p_model - market_price
        if edge > 0.03:
            buy = True
        else:
            buy = False

        if buy:
            # If market resolved YES (price ~1.0), we win; if NO (price ~0.0), we lose
            # For resolved markets, payout = 1.0 if winner, 0.0 if not
            payout = 1.0 if is_winner else 0.0
            cost = market_price  # what we would have paid
            pnl = payout - cost
            trades.append({
                'bracket': label,
                'p_model': p_model,
                'market_price': market_price,
                'edge': edge,
                'is_winner': is_winner,
                'pnl': pnl,
            })

    n_buys = len(trades)
    n_wins = sum(1 for t in trades if t['is_winner'])
    n_losses = n_buys - n_wins
    total_pnl = sum(t['pnl'] for t in trades)

    print(f"\n  SIMULATED TRADES (edge > 0.03):")
    print(f"    Total buys: {n_buys}  Wins: {n_wins}  Losses: {n_losses}")
    print(f"    Total P&L: {total_pnl:+.4f} (per $1 invested at market price)")
    for t in trades:
        print(f"      {t['bracket']:12s}  p_model={t['p_model']:.4f}  price={t['market_price']:.4f}  edge={t['edge']:+.4f}  {'WIN' if t['is_winner'] else 'LOSS'}  pnl={t['pnl']:+.4f}")

    # Store result
    results.append({
        'station': station_icao,
        'date': target_date,
        'actual_temp': actual_temp,
        'raw_mean': raw_mean,
        'adj_mean': adj_mean,
        'bias': bias,
        'n_models': len(model_temps),
        'n_brackets': len(labels),
        'winner_idx': winner_idx,
        'winner_label': winner_label,
        'winner_p_model': winner_p_model,
        'winner_market_price': winner_market_price,
        'top1_idx': top1_idx,
        'top1_label': labels[top1_idx],
        'top1_p_model': probs[top1_idx],
        'top1_match': top1_match,
        'top2_match': top2_match,
        'n_buys': n_buys,
        'n_wins': n_wins,
        'n_losses': n_losses,
        'total_pnl': total_pnl,
        'trades': trades,
    })

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n\n{'=' * 100}")
print("BACKTEST SUMMARY")
print(f"{'=' * 100}")

valid = [r for r in results if 'error' not in r]
errors = [r for r in results if 'error' in r]

if errors:
    print(f"\n  Errors: {len(errors)}")
    for e in errors:
        print(f"    {e['station']} {e['date']}: {e['error']}")

if not valid:
    print("\n  No valid results to summarize.")
    sys.exit(1)

n = len(valid)
top1_hits = sum(1 for r in valid if r['top1_match'])
top2_hits = sum(1 for r in valid if r['top2_match'])
total_buys = sum(r['n_buys'] for r in valid)
total_wins = sum(r['n_wins'] for r in valid)
total_losses = sum(r['n_losses'] for r in valid)
total_pnl = sum(r['total_pnl'] for r in valid)
avg_winner_p = sum(r['winner_p_model'] for r in valid) / n
avg_adj_error = sum(abs(r['adj_mean'] - r['actual_temp']) for r in valid) / n
avg_raw_error = sum(abs(r['raw_mean'] - r['actual_temp']) for r in valid) / n

print(f"\n  Markets analyzed:  {n}")
print(f"  Top-1 accuracy:   {top1_hits}/{n}  ({top1_hits/n*100:.1f}%)")
print(f"  Top-2 accuracy:   {top2_hits}/{n}  ({top2_hits/n*100:.1f}%)")
print(f"  Avg P(actual):    {avg_winner_p:.4f}")
print(f"  Avg raw error:    {avg_raw_error:.2f}°C")
print(f"  Avg adj error:     {avg_adj_error:.2f}°C  (after bias correction)")
print(f"  Total buys:        {total_buys}")
print(f"  Wins:              {total_wins}")
print(f"  Losses:            {total_losses}")
print(f"  Win rate:          {total_wins/total_buys*100:.1f}%" if total_buys > 0 else "  Win rate: N/A")
print(f"  Total P&L:         {total_pnl:+.4f} (per $1 at market price)")

print(f"\n{'─' * 100}")
print(f"{'Station':8s} {'Date':12s} {'Actual':>7s} {'Raw μ':>7s} {'Adj μ':>7s} {'Bias':>6s} {'Winner':12s} {'P(act)':>7s} {'Top-1':12s} {'T1 Match':>10s} {'T2':>5s} {'Buys':>5s} {'W/L':>5s} {'P&L':>8s}")
print(f"{'─' * 100}")
for r in valid:
    print(f"{r['station']:8s} {r['date']:12s} {r['actual_temp']:>6d}°C {r['raw_mean']:>6.1f}°C {r['adj_mean']:>6.1f}°C {r['bias']:>+5.1f}°C {r['winner_label']:12s} {r['winner_p_model']:>7.4f} {r['top1_label']:12s} {'✓' if r['top1_match'] else '✗':>10s} {'✓' if r['top2_match'] else '✗':>5s} {r['n_buys']:>5d} {r['n_wins']:>2d}/{r['n_losses']:<2d} {r['total_pnl']:>+8.4f}")
print(f"{'─' * 100}")

print(f"\nDone.")