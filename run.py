#!/usr/bin/env python3
"""
Soak Test Runner — the full trading pipeline in dry-run mode.

Cycles: discover → map → forecast → EV → Kelly → (dry-run order) → log → sleep → repeat

In dry-run mode, orders are logged but NOT submitted to the CLOB.
This proves the pipeline is stable for hours without risking funds.

Usage:
  python3 run.py                    # dry-run, runs forever (Ctrl+C to stop)
  python3 run.py --cycles 5         # dry-run, stop after 5 cycles
  python3 run.py --live             # LIVE mode (places real orders — dangerous)
  python3 run.py --interval 60      # override poll interval (seconds)
"""

import sys
import os
import time
import argparse
import yaml
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)


def load_settings():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def run_cycle(cycle_num, settings, audit, dry_run=True):
    """Run one complete pipeline cycle. Returns True if cycle completed."""
    from src.data.gamma_client import discover_weather_events, is_weather_event
    from src.data.station_lookup import StationLookup
    from src.data.weather_client import fetch_station_forecast
    from src.data.clob_reader import get_orderbook
    from src.strategy.market_mapper import map_weather_market
    from src.strategy.forecast_prob import compute_bracket_probabilities, best_opportunity
    from src.strategy.ev_engine import filter_opportunities
    from src.strategy.kelly import size_position

    print(f"\n{'='*60}")
    print(f"CYCLE {cycle_num} — {datetime.now(timezone.utc).isoformat()[:19]}")
    print(f"{'='*60}")

    # --- Stage 1: Discover weather markets ---
    print("\n[1] Discovering weather markets...")
    try:
        queries = settings.get("discovery", {}).get("search_queries", ["temperature"])
        min_vol = settings.get("discovery", {}).get("min_event_volume", 5000)
        events = discover_weather_events(queries=queries, min_volume=min_vol, active_only=True)
        weather = [e for e in events if is_weather_event(e)]
        print(f"  Found {len(events)} total, {len(weather)} genuine weather events (active)")
        audit.log_discovery(cycle_num, len(weather), [{"title": e.title, "slug": e.slug} for e in weather])
    except Exception as e:
        print(f"  ERROR: {e}")
        audit.log_error(cycle_num, str(e), "discovery")
        return True  # cycle still "completed" — error handled, not crash

    if not weather:
        print("  No active weather markets. Going dormant.")
        audit.log_info(cycle_num, "no active weather markets — dormant")
        # Still log balance for monitoring
        try:
            from src.execution.clob_trader import ClobTrader
            trader = ClobTrader()
            bal = trader.get_balance_allowance()
            balance = float(bal.get("balance", "0")) / 1e6 if bal else 0
            orders = trader.get_open_orders()
            audit.log_balance(cycle_num, balance, len(orders))
            print(f"  Balance: ${balance:.2f}  Open orders: {len(orders)}")
        except Exception as e:
            print(f"  Balance check error: {e}")
        return True

    # --- Stage 2: Map markets to stations + parse brackets ---
    print("\n[2] Mapping markets to stations...")
    lookup = StationLookup("config/stations.yaml")
    mapped_markets = []
    for evt in weather:
        market = map_weather_market(evt, lookup)
        if market:
            mapped_markets.append(market)
            print(f"  {market.title} → {market.station_icao} ({len(market.brackets)} brackets)")
        else:
            print(f"  SKIP: {evt.title} (no station found or no brackets)")

    if not mapped_markets:
        print("  No markets could be mapped. Dormant.")
        audit.log_info(cycle_num, "no mappable markets")
        return True

    # --- Stage 3: Fetch forecasts for each mapped market ---
    print("\n[3] Fetching multi-model forecasts...")
    trading_params = settings.get("trading", {})
    clip_min = trading_params.get("p_model_clip_min", 0.01)
    clip_max = trading_params.get("p_model_clip_max", 0.99)
    min_ens = trading_params.get("min_ensemble_runs", 30)

    for market in mapped_markets:
        station = lookup.get_station(market.station_icao)
        # Use market's target date if available, otherwise today
        target_date = market.target_date if market.target_date else datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            ensemble = fetch_station_forecast(station, target_date)

            # --- MIN_MODELS GUARD: skip trading on insufficient forecast data ---
            # Check both model count AND actual temp data count.
            min_models = trading_params.get("min_models", 2)
            n_models = len(ensemble.models)
            if market.metric == "low":
                temps_log = ensemble.min_temps_all
            else:
                temps_log = ensemble.max_temps_all
            n_temps = len(temps_log)
            if n_models < min_models or n_temps < min_models:
                print(f"  [GUARD] SKIPPING {market.station_icao}: {n_models} models, {n_temps} temps (need {min_models})")
                audit.log_info(cycle_num, f"min_models guard: {market.station_icao} {n_models}m/{n_temps}t — skipping")
                continue

            if market.metric == "low":
                mean, stdev = ensemble.consensus_min()
                metric_label = "min"
            else:
                mean, stdev = ensemble.consensus()
                metric_label = "max"
            print(f"  {market.station_icao}: {n_models} models, {metric_label}={mean:.1f}±{stdev:.1f}{market.units} ({market.metric})")
            audit.log_forecast(cycle_num, market.station_icao, target_date, temps_log, mean, stdev,
                               metric=market.metric, market_slug=market.slug)

            # --- Stage 4: Compute per-bracket probabilities ---
            probs = compute_bracket_probabilities(
                ensemble, market,
                min_ensemble=min_ens,
                clip_min=clip_min, clip_max=clip_max,
            )

            # --- Stage 5: Compute EV and filter ---
            ev_threshold = trading_params.get("ev_threshold", 0.03)
            min_ask_depth = trading_params.get("min_ask_depth_shares", 50)

            # Fetch orderbooks ONCE per bracket — reuse for EV, liquidity gate, and Kelly
            orderbooks = {}  # bracket_label -> OrderBook
            for prob in probs:
                try:
                    book = get_orderbook(prob.bracket.yes_token)
                    orderbooks[prob.bracket.label] = book
                except Exception:
                    pass

            ask_prices = {}
            for label, book in orderbooks.items():
                ask_prices[label] = book.best_ask
            # Fallback for brackets where orderbook fetch failed
            for prob in probs:
                if prob.bracket.label not in ask_prices:
                    ask_prices[prob.bracket.label] = prob.bracket.yes_price

            opportunities = filter_opportunities(probs, ask_prices, ev_threshold)

            print(f"  {len(probs)} brackets, {len(opportunities)} pass EV filter (>={ev_threshold})")
            if len(opportunities) == 0 and len(probs) > 0:
                max_ask = max(ask_prices.values(), default=0.0)
                if max_ask >= 0.95:
                    print(f"    [GUARD] Market resolved — top bracket at {max_ask:.2f}, skipping all orders")
                    audit.log_info(cycle_num, f"resolved market guard: {market.title} top ask={max_ask:.2f}")
            for opp in opportunities:
                audit.log_ev(cycle_num, opp.bracket_label, opp.p_model, opp.p_market, opp.ev_per_share, opp.passes_filter,
                             market_slug=market.slug, station=market.station_icao)
                print(f"    {opp.bracket_label}: p_model={opp.p_model:.3f} p_market={opp.p_market:.3f} EV={opp.ev_per_share:.4f}")

            # --- Stage 6: Kelly sizing + liquidity gate + (dry-run) order ---
            if opportunities:
                bankroll = trading_params.get("bankroll_usdc", 50.0)
                kelly_frac = trading_params.get("kelly_fraction", 0.25)
                market_cap = trading_params.get("single_market_cap", 0.05)

                for opp in opportunities:
                    book = orderbooks.get(opp.bracket_label)

                    # Liquidity gate: check available shares at or below our limit price
                    available_shares = 0.0
                    if book:
                        available_shares = book.available_depth_at_price(opp.price)

                    if available_shares < min_ask_depth:
                        print(f"    SKIP: {opp.bracket_label} — only {available_shares:.0f} shares at ≤${opp.price:.2f} (need {min_ask_depth})")
                        audit.log_info(cycle_num, f"liquidity skip: {market.slug} {opp.bracket_label} depth={available_shares:.0f}<{min_ask_depth}")
                        continue

                    # Cap ask_depth for Kelly to what's actually available
                    ask_depth = min(available_shares, book.ask_depth_top3 if book else 0)

                    kelly_result = size_position(
                        p_model=opp.p_model,
                        price=opp.price,
                        bankroll=bankroll,
                        kelly_fraction=kelly_frac,
                        single_market_cap=market_cap,
                        ask_depth_top3=ask_depth,
                        bracket_label=opp.bracket_label,
                    )

                    print(f"    Kelly: {opp.bracket_label} → ${kelly_result.final_dollars:.2f} ({kelly_result.final_shares:.1f} shares) [{kelly_result.binding_constraint}]")

                    if kelly_result.final_dollars > 0 and kelly_result.final_shares >= 5:
                        if dry_run:
                            print(f"    DRY-RUN: BUY {kelly_result.final_shares:.1f} @ {opp.price:.2f} (depth: {available_shares:.0f} shares)")
                            audit.log_order(cycle_num, opp.bracket_label, "BUY", opp.price, kelly_result.final_shares, True,
                                            status="dry_run", market_slug=market.slug, station=market.station_icao)
                        else:
                            # LIVE order
                            from src.execution.clob_trader import ClobTrader, TradeOrder
                            try:
                                trader = ClobTrader()
                                order = TradeOrder(
                                    token_id=opp.token_id,
                                    side="BUY",
                                    price=opp.price,
                                    size=kelly_result.final_shares,
                                    market_slug=market.slug,
                                    ev_per_share=opp.ev_per_share,
                                    kelly_fraction=kelly_frac,
                                    p_model=opp.p_model,
                                )
                                result = trader.place_limit_order(order, dry_run=False)
                                print(f"    LIVE: {result.status} order_id={result.order_id}")
                                audit.log_order(cycle_num, opp.bracket_label, "BUY", opp.price, kelly_result.final_shares, False,
                                                result.order_id, result.status, result.error,
                                                market_slug=market.slug, station=market.station_icao)
                            except Exception as e:
                                print(f"    LIVE ERROR: {e}")
                                audit.log_order(cycle_num, opp.bracket_label, "BUY", opp.price, kelly_result.final_shares, False,
                                                error=str(e), market_slug=market.slug, station=market.station_icao)
                    else:
                        print(f"    SKIP: size too small or no edge")

        except Exception as e:
            print(f"  FORECAST ERROR for {market.station_icao}: {e}")
            audit.log_error(cycle_num, str(e), f"forecast:{market.station_icao}")

    # --- Balance check ---
    print("\n[4] Balance check...")
    try:
        from src.execution.clob_trader import ClobTrader
        trader = ClobTrader()
        bal = trader.get_balance_allowance()
        balance = float(bal.get("balance", "0")) / 1e6 if bal else 0
        orders = trader.get_open_orders()
        print(f"  Balance: ${balance:.2f}  Open orders: {len(orders)}")
        audit.log_balance(cycle_num, balance, len(orders))
    except Exception as e:
        print(f"  Balance check error: {e}")
        audit.log_error(cycle_num, str(e), "balance_check")

    audit.log_cycle(cycle_num, status="completed")
    print(f"\n  Cycle {cycle_num} complete.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Polymarket Weather Trader — Soak Test")
    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 = forever)")
    parser.add_argument("--live", action="store_true", help="LIVE mode — places real orders")
    parser.add_argument("--interval", type=int, default=0, help="Poll interval override (seconds)")
    args = parser.parse_args()

    settings = load_settings()
    dry_run = not args.live

    soak_settings = settings.get("soak", {})
    interval = args.interval if args.interval > 0 else soak_settings.get("poll_interval_seconds", 300)
    max_cycles = args.cycles if args.cycles > 0 else soak_settings.get("max_cycles", 0)

    log_path = soak_settings.get("log_path", "logs/soak_audit.jsonl")
    if dry_run:
        log_path = log_path.replace(".jsonl", "_dryrun.jsonl")

    from src.risk.audit_log import AuditLog
    audit = AuditLog(log_path)

    print("=" * 60)
    print("POLYMARKET WEATHER TRADER — SOAK TEST")
    print("=" * 60)
    print(f"  Mode: {'DRY-RUN' if dry_run else 'LIVE (DANGEROUS)'}")
    print(f"  Interval: {interval}s")
    print(f"  Max cycles: {'∞' if max_cycles == 0 else max_cycles}")
    print(f"  Log: {log_path}")
    print(f"  Bankroll: ${settings.get('trading', {}).get('bankroll_usdc', 50):.2f}")
    print(f"  Kelly fraction: {settings.get('trading', {}).get('kelly_fraction', 0.25)}")
    print(f"  EV threshold: ${settings.get('trading', {}).get('ev_threshold', 0.03)}")
    print("=" * 60)

    cycle = 1
    try:
        while True:
            run_cycle(cycle, settings, audit, dry_run)

            if max_cycles > 0 and cycle >= max_cycles:
                print(f"\nReached max cycles ({max_cycles}). Stopping.")
                break

            cycle += 1
            print(f"\n  Sleeping {interval}s...")
            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped by user after {cycle} cycles.")
        print(f"Audit log: {log_path}")
        # Show recent log entries
        recent = audit.read_recent(5)
        if recent:
            print(f"\nLast {len(recent)} log entries:")
            for entry in recent:
                print(f"  [{entry.get('type', '?')}] {entry.get('timestamp', '?')[:19]}")


if __name__ == "__main__":
    main()