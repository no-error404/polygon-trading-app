#!/usr/bin/env python3
"""
Sniper Bot — bias-corrected model + patient limit orders.

Key insight from backtest:
  - Markets don't price most brackets until ~12h before resolution
  - At 12h: market slightly overpriced (price > model p)
  - At 6h: market dips below model p (EDGE WINDOW)
  - By 3h: resolved

Strategy:
  1. For each active weather market, compute bias-corrected probabilities
  2. Identify the model's top-1 bracket (89% accuracy in backtest)
  3. Set a limit buy order at (model_p - ev_threshold)
  4. Monitor price — if ask drops below our limit, the order fills
  5. If the price never dips, we don't trade (patient)

This replaces the old "buy whatever has edge right now" approach with
"wait for the market to come to us during the pre-resolution dip."

Usage:
  python3 run_sniper.py                    # dry-run
  python3 run_sniper.py --live             # LIVE (places real orders)
  python3 run_sniper.py --interval 60      # check every 60s (faster for dip catching)
"""

import sys
import os
import time
import argparse
import yaml
import json
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

STATE_FILE_LIVE = PROJECT_ROOT / ".sniper_state.json"
STATE_FILE_DRY = PROJECT_ROOT / ".sniper_state_dryrun.json"


def load_state(dry_run=False):
    """Load persistent state across cycles (bought_keys, cumulative_deployed)."""
    path = STATE_FILE_DRY if dry_run else STATE_FILE_LIVE
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"bought_keys": [], "cumulative_deployed": 0.0}


def save_state(bought_keys, cumulative_deployed, dry_run=False):
    """Persist state so it survives across cycles and restarts."""
    path = STATE_FILE_DRY if dry_run else STATE_FILE_LIVE
    try:
        with open(path, "w") as f:
            json.dump({
                "bought_keys": list(bought_keys),
                "cumulative_deployed": round(cumulative_deployed, 6),
            }, f, indent=2)
    except OSError as e:
        print(f"  [WARN] Failed to save state: {e}")


def load_settings():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def run_sniper_cycle(cycle_num, settings, audit, dry_run=True, max_deployment=None):
    """Run one sniper cycle.

    Args:
        max_deployment: if set, cap total dollars deployed this session at this amount.
                        Trades are skipped once cumulative deployment reaches this cap.
    """
    from src.data.gamma_client import discover_weather_events, is_weather_event
    from src.data.station_lookup import StationLookup
    from src.data.weather_client import fetch_station_forecast
    from src.data.clob_reader import get_orderbook
    from src.strategy.market_mapper import map_weather_market
    from src.strategy.bias_corrected import (
        BiasCorrectedConfig, compute_bias_corrected_probabilities,
        evaluate_opportunity, get_station_bias,
    )
    from src.strategy.kelly import size_position

    print(f"\n{'='*60}")
    print(f"SNIPER CYCLE {cycle_num} — {datetime.now(timezone.utc).isoformat()[:19]}")
    print(f"{'='*60}")

    # --- Stage 0: REMOVED PositionManager ---
    # The PositionManager was causing fire-sales at near-zero prices ($0.005-$0.011).
    # The sniper strategy is HOLD-TO-RESOLUTION — never sell before resolution.
    # Positions resolve automatically via Polymarket's UMA oracle.

    config = BiasCorrectedConfig.from_yaml(settings)
    min_ask_depth = settings.get("trading", {}).get("min_ask_depth_shares", 50)

    # --- Stage 1: Discover weather markets ---
    print("\n[1] Discovering weather markets...")
    try:
        queries = settings.get("discovery", {}).get("search_queries", ["temperature"])
        min_vol = settings.get("discovery", {}).get("min_event_volume", 5000)
        events = discover_weather_events(queries=queries, min_volume=min_vol, active_only=True)
        weather = [e for e in events if is_weather_event(e)]
        print(f"  Found {len(events)} total, {len(weather)} genuine weather events")
    except Exception as e:
        print(f"  ERROR: {e}")
        audit.log_error(cycle_num, str(e), "discovery")
        return True

    if not weather:
        print("  No active weather markets. Dormant.")
        return True

    # --- Stage 2: Map to stations ---
    print("\n[2] Mapping markets to stations...")
    lookup = StationLookup("config/stations.yaml")
    mapped_markets = []
    for evt in weather:
        market = map_weather_market(evt, lookup)
        if market:
            mapped_markets.append(market)
            print(f"  {market.title} → {market.station_icao} ({len(market.brackets)} brackets)")
        else:
            print(f"  SKIP: {evt.title} (no station or brackets)")

    if not mapped_markets:
        print("  No mappable markets.")
        return True

    # --- Stage 3: Compute bias-corrected probabilities + identify top picks ---
    print("\n[3] Computing bias-corrected probabilities...")
    trading_params = settings.get("trading", {})
    bankroll = trading_params.get("bankroll_usdc", 50.0)
    kelly_frac = trading_params.get("kelly_fraction", 0.25)
    market_cap = trading_params.get("single_market_cap", 0.05)
    ev_threshold = trading_params.get("ev_threshold", 0.03)
    limit_price_fraction = trading_params.get("limit_price_fraction", 0.97)
    max_hours = trading_params.get("max_hours_to_resolution", 12)

    # --- Persistent state across cycles (survives restarts) ---
    state = load_state(dry_run=dry_run)
    cumulative_deployed = state.get("cumulative_deployed", 0.0)
    bought_keys = set(state.get("bought_keys", []))
    deployed_trades = []  # list of dicts for logging this cycle only

    # Early exit if deployment cap already reached — RETURN, don't fall through
    if max_deployment is not None and cumulative_deployed >= max_deployment:
        print(f"  [CAP] Deployment cap already reached (${cumulative_deployed:.2f} / ${max_deployment:.2f}) — skipping all buys this cycle")
        # Still log balance for monitoring
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
        audit.log_cycle(cycle_num, status="completed (cap reached)")
        print(f"\n  Sniper cycle {cycle_num} complete (cap reached).")
        return True

    sniper_targets = []  # list of dicts with market, bracket, p_model, limit_price

    for market in mapped_markets:
        station = lookup.get_station(market.station_icao)
        target_date = market.target_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            ensemble = fetch_station_forecast(station, target_date)

            # --- MIN_MODELS GUARD: skip trading on insufficient forecast data ---
            # Check both model count AND actual temp data count (a model can
            # "succeed" but return empty hourly_temps, which would leave
            # max_temps_all empty while models dict is populated).
            min_models = trading_params.get("min_models", 2)
            n_models = len(ensemble.models)
            if market.metric == "low":
                temps = ensemble.min_temps_all
            else:
                temps = ensemble.max_temps_all
            n_temps = len(temps)
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

            bias = get_station_bias(market.station_icao)
            adj_mean = mean + bias
            print(f"  {market.station_icao}: {n_models} models, {metric_label}={mean:.1f}±{stdev:.1f}{market.units} (adj: {adj_mean:.1f}±{stdev:.1f}, bias={bias:+.1f})")

            # Build bracket arrays for bias-corrected computation
            bucket_labels = [b.label for b in market.brackets]
            bucket_lows = [b.low for b in market.brackets]
            bucket_highs = [b.high for b in market.brackets]

            # Compute bias-corrected probabilities
            probs = compute_bias_corrected_probabilities(
                temps, market.station_icao,
                bucket_labels, bucket_lows, bucket_highs, config,
            )

            # Rank brackets by model probability
            ranked = sorted(range(len(probs)), key=lambda x: probs[x], reverse=True)
            top_idx = ranked[0]
            top_label = bucket_labels[top_idx]
            top_p = probs[top_idx]

            # Fetch orderbook for top bracket
            bracket = market.brackets[top_idx]
            try:
                book = get_orderbook(bracket.yes_token)
                best_ask = book.best_ask
                # Depth at best_ask — used for liquidity assessment
                available = book.available_depth_at_price(best_ask)
            except Exception as e:
                best_ask = bracket.yes_price
                available = 0

            # Skip if market already resolved (ask >= 0.95)
            if best_ask >= 0.95:
                print(f"    [GUARD] {top_label} resolved at {best_ask:.2f}, skipping")
                continue

            # Skip phantom orders — stale/dead liquidity at near-zero prices.
            # Use the config's min_ask_price (default 0.02) so the guard
            # stays in sync with bias_corrected.evaluate_opportunity's filter.
            min_ask_price = config.min_ask_price
            if best_ask < min_ask_price:
                print(f"    [GUARD] {top_label} ask={best_ask:.3f} < min_ask_price {min_ask_price} — phantom order, skipping")
                continue

            # Time-to-resolution filter: only trade markets resolving within max_hours
            if target_date:
                try:
                    from datetime import datetime as _dt
                    resolve_dt = _dt.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    hours_to_resolve = (resolve_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_to_resolve > max_hours:
                        print(f"    [SKIP] {target_date} is {hours_to_resolve:.0f}h away > {max_hours}h max — too early")
                        continue
                    if hours_to_resolve < -24:
                        print(f"    [SKIP] {target_date} already passed ({hours_to_resolve:.0f}h)")
                        continue
                except ValueError:
                    pass  # can't parse date, allow through

            # Set limit price = ask * limit_price_fraction (tight to ask, not model_p - ev_threshold)
            # Old formula (top_p - ev_threshold) placed limits ~9% below ask — never filled.
            # New formula: 97% of ask = max 3% below, much more likely to fill on dips.
            if best_ask > 0 and top_p > best_ask:
                # We have edge (model > ask): set limit just below ask
                limit_price = best_ask * limit_price_fraction
            else:
                # No edge: use old formula as fallback (won't trade anyway)
                limit_price = top_p - ev_threshold

            # Clip limit price to valid CLOB range [0.02, 0.95]
            # CLOB rejects 0.0 and 1.0 — must be strictly within (0, 1)
            limit_price = max(0.02, min(0.95, limit_price))

            ev = top_p - best_ask if best_ask > 0 else 0
            print(f"    Top pick: {top_label} (p_model={top_p:.3f}) | ask={best_ask:.3f} | depth={available:.0f} | limit={limit_price:.3f} | EV={ev:+.3f}")

            # We have edge (model prob > ask). Place a maker limit at 97% of ask.
            # The order sits on the book and fills when ask dips 3%. No taker orders.
            if top_p > best_ask and best_ask > 0 and available >= min_ask_depth:
                # Clamp to valid CLOB price range [0.01, 0.99] — CLOB rejects 0.0 and 1.0
                order_price = max(0.01, min(0.99, round(limit_price, 2)))
                print(f"    Placing maker limit at {order_price:.2f} (ask={best_ask:.3f}, {limit_price_fraction:.0%} of ask)")

                # Dedup guard — don't buy the same station+bracket twice
                trade_key = f"{market.station_icao}:{top_label}"
                if trade_key in bought_keys:
                    print(f"    SKIP: already bought {trade_key} this cycle")
                    continue

                # Deployment cap guard
                if max_deployment is not None and cumulative_deployed >= max_deployment:
                    print(f"    SKIP: deployment cap reached (${cumulative_deployed:.2f} / ${max_deployment:.2f})")
                    continue

                kelly_result = size_position(
                    p_model=top_p,
                    price=order_price,
                    bankroll=bankroll,
                    kelly_fraction=kelly_frac,
                    single_market_cap=market_cap,
                    ask_depth_top3=min(available, book.ask_depth_top3 if book else 0),
                    bracket_label=top_label,
                )

                if kelly_result.final_dollars > 0 and kelly_result.final_shares >= 5:
                    # Clamp trade size to remaining deployment budget
                    trade_dollars = kelly_result.final_dollars
                    if max_deployment is not None:
                        remaining = max_deployment - cumulative_deployed
                        if trade_dollars > remaining:
                            trade_dollars = remaining
                            # Recompute shares from clamped dollars
                            kelly_result.final_dollars = trade_dollars
                            kelly_result.final_shares = trade_dollars / order_price if order_price > 0 else 0
                            print(f"    [CAP] Clamped to ${trade_dollars:.2f} ({kelly_result.final_shares:.1f} shares) — deployment budget")

                    if kelly_result.final_shares < 5:
                        print(f"    SKIP: clamped size {kelly_result.final_shares:.1f} < 5 minimum")
                        continue

                    if dry_run:
                        print(f"    DRY-RUN: BUY {kelly_result.final_shares:.1f} @ {order_price:.2f} = ${kelly_result.final_dollars:.2f} (depth: {available:.0f} shares) [{kelly_result.binding_constraint}]")
                        audit.log_order(cycle_num, top_label, "BUY", order_price, kelly_result.final_shares, True,
                                        status="dry_run", market_slug=market.slug, station=market.station_icao)
                        cumulative_deployed += kelly_result.final_dollars
                        bought_keys.add(trade_key)
                        deployed_trades.append({
                            "station": market.station_icao, "bracket": top_label,
                            "price": order_price, "shares": kelly_result.final_shares,
                            "dollars": kelly_result.final_dollars,
                        })
                    else:
                        # LIVE ORDER SUBMISSION
                        from src.execution.clob_trader import ClobTrader, TradeOrder
                        try:
                            trader = ClobTrader()
                            order = TradeOrder(
                                token_id=bracket.yes_token,
                                side="BUY",
                                price=order_price,
                                size=round(kelly_result.final_shares, 2),
                                market_slug=market.slug,
                                market_question=market.title,
                                ev_per_share=ev,
                                kelly_fraction=kelly_result.f_fractional,
                                p_model=top_p,
                            )
                            result = trader.place_limit_order(
                                order=order,
                                tick_size=book.tick_size,
                                min_size=book.min_order_size,
                                mid_price=book.mid,
                                dry_run=False,
                            )
                            if result.success:
                                print(f"    ✓ LIVE ORDER PLACED: {kelly_result.final_shares:.1f} @ {order_price:.2f} = ${kelly_result.final_dollars:.2f} | order_id={result.order_id}")
                                audit.log_order(cycle_num, top_label, "BUY", order_price, kelly_result.final_shares, False,
                                                status=result.status, market_slug=market.slug, station=market.station_icao,
                                                order_id=result.order_id)
                                cumulative_deployed += kelly_result.final_dollars
                                bought_keys.add(trade_key)
                                deployed_trades.append({
                                    "station": market.station_icao, "bracket": top_label,
                                    "price": order_price, "shares": kelly_result.final_shares,
                                    "dollars": kelly_result.final_dollars,
                                    "order_id": result.order_id,
                                })
                            else:
                                print(f"    ✗ ORDER FAILED: {result.error}")
                                audit.log_error(cycle_num, f"Order failed: {result.error}", f"order:{market.station_icao}:{top_label}")
                        except Exception as oe:
                            print(f"    ✗ ORDER EXCEPTION: {oe}")
                            audit.log_error(cycle_num, str(oe), f"order:{market.station_icao}:{top_label}")
                else:
                    print(f"    SKIP: size too small (${kelly_result.final_dollars:.2f})")
            else:
                # Price not there yet — set a sniper target
                reason = ""
                if best_ask > limit_price:
                    reason = f"ask {best_ask:.3f} > limit {limit_price:.3f} (waiting for dip)"
                elif available < min_ask_depth:
                    reason = f"depth {available:.0f} < {min_ask_depth} (waiting for liquidity)"
                elif best_ask == 0:
                    reason = "no asks yet (market not pricing this bracket)"
                else:
                    reason = "unknown"

                print(f"    → SNIPING: limit @ {limit_price:.3f} | {reason}")
                sniper_targets.append({
                    "market_slug": market.slug,
                    "station": market.station_icao,
                    "bracket": top_label,
                    "token": bracket.yes_token,
                    "p_model": top_p,
                    "limit_price": limit_price,
                    "current_ask": best_ask,
                    "available_depth": available,
                    "reason": reason,
                })

        except Exception as e:
            print(f"  FORECAST ERROR for {market.station_icao}: {e}")
            audit.log_error(cycle_num, str(e), f"forecast:{market.station_icao}")

    # --- Summary ---
    print(f"\n[4] Sniper summary...")
    print(f"  Targets being watched: {len(sniper_targets)}")
    for t in sniper_targets:
        print(f"    {t['station']} {t['bracket']:10s} | p_model={t['p_model']:.3f} | limit={t['limit_price']:.3f} | ask={t['current_ask']:.3f} | {t['reason']}")

    if deployed_trades:
        print(f"\n  Trades deployed this cycle: {len(deployed_trades)}")
        for t in deployed_trades:
            oid = t.get("order_id", "dry_run")
            print(f"    {t['station']} {t['bracket']:10s} | {t['shares']:.1f} @ {t['price']:.2f} = ${t['dollars']:.2f} | {oid}")
        print(f"  Cumulative deployed: ${cumulative_deployed:.2f}" + (f" / ${max_deployment:.2f}" if max_deployment else ""))

    # Balance check
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

    # --- Persist state for next cycle ---
    save_state(bought_keys, cumulative_deployed, dry_run=dry_run)

    audit.log_cycle(cycle_num, status="completed")
    print(f"\n  Sniper cycle {cycle_num} complete.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Polymarket Weather Sniper — Bias-Corrected + Dip Hunting")
    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 = forever)")
    parser.add_argument("--live", action="store_true", help="LIVE mode — places real orders")
    parser.add_argument("--interval", type=int, default=0, help="Poll interval (seconds)")
    parser.add_argument("--max-deployment", type=float, default=None,
                        help="Cap total dollars deployed (e.g. 10.0 = stop after $10 in orders)")
    args = parser.parse_args()

    settings = load_settings()
    dry_run = not args.live

    interval = args.interval if args.interval > 0 else 120  # default 2min for sniper
    max_cycles = args.cycles if args.cycles > 0 else 0

    log_path = settings.get("soak", {}).get("log_path", "logs/soak_audit.jsonl")
    if dry_run:
        log_path = log_path.replace(".jsonl", "_sniper_dryrun.jsonl")
    else:
        log_path = log_path.replace(".jsonl", "_sniper_live.jsonl")

    from src.risk.audit_log import AuditLog
    audit = AuditLog(log_path)

    print("=" * 60)
    print("POLYMARKET WEATHER SNIPER — Bias-Corrected + Dip Hunting")
    print("=" * 60)
    print(f"  Mode: {'DRY-RUN' if dry_run else 'LIVE (REAL MONEY)'}")
    print(f"  Interval: {interval}s")
    print(f"  Max cycles: {'∞' if max_cycles == 0 else max_cycles}")
    print(f"  Max deployment: ${args.max_deployment:.2f}" if args.max_deployment else "  Max deployment: unlimited")
    print(f"  Log: {log_path}")
    print(f"  Bankroll: ${settings.get('trading', {}).get('bankroll_usdc', 50):.2f}")
    print(f"  Kelly fraction: {settings.get('trading', {}).get('kelly_fraction', 0.25)}")
    print(f"  EV threshold: ${settings.get('trading', {}).get('ev_threshold', 0.03)}")
    print(f"  Bias correction: per-station (RKSI=+2.9, ZSPD=+2.5, EGLC=+0.8, etc.)")
    print("=" * 60)

    cycle = 1
    try:
        while True:
            run_sniper_cycle(cycle, settings, audit, dry_run, max_deployment=args.max_deployment)

            if max_cycles > 0 and cycle >= max_cycles:
                print(f"\nReached max cycles ({max_cycles}). Stopping.")
                break

            cycle += 1
            print(f"\n  Sleeping {interval}s...")
            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped by user after {cycle} cycles.")
        print(f"Audit log: {log_path}")


if __name__ == "__main__":
    main()