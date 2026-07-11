# Polymarket Weather Markets — Live Data Ingest & Assessment

Date of ingest: 2026-07-06
Source: Gamma API (gamma-api.polymarket.com), public, no auth.
Purpose: evaluate whether live Polymarket weather markets are suitable
for (a) a systematic trading system and (b) a teaching tutorial.

---

## 1. What we found

22 weather-related events across 5 query terms (temperature, weather,
snow, rainfall, space weather). Breakdown by type:

| Type | Events | Status | Example volumes |
|---|---|---|---|
| City max-temp bracket (daily) | 4 | All CLOSED | $483K (NYC), $758K (Seoul), $679K (Paris) |
| NYC snowfall bracket (weekend) | 2 | All CLOSED | $1.45M (Jan 24-26), $22K (Feb 21-23) |
| Space-weather event counts | 5 | All CLOSED | $9K-$105K |
| Non-weather false positives (SNOW ticker, Beast Games, primaries) | 11 | — | — |

KEY FINDING: zero ACTIVE weather markets at ingest time. Every genuine
weather market is CLOSED. This is the single most important fact for
both the system and the tutorial, and it dominates the assessment below.

---

## 2. Market microstructure — confirmed from real data

### 2.1 Bracket structure (temperature)
NYC April 16 event, 11 markets, 2°F buckets:
  <=77 | 78-79 | 80-81 | 82-83 | 84-85 | 86-87 | 88-89 | 90-91 | 92-93 | 94-95 | >=96
Seoul April 17 event, 11 markets, 1°C buckets:
  <=8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | >=18

This is the negative-risk multi-outcome structure described in
PHASE1_FOUNDATIONS.md — confirmed in production. Each bucket is its own
binary market with its own conditionId and clobTokenIds pair.

### 2.2 Resolution source — CONFIRMED, this is critical
NYC:  "highest temperature recorded at the LaGuardia Airport Station"
      Resolution source: Wunderground history for KLGA
      URL pattern: wunderground.com/history/daily/us/ny/new-york-city/KLGA

Seoul: "highest temperature recorded at the Incheon Intl Airport Station"
       Resolution source: Wunderground history for RKSI
       URL pattern: wunderground.com/history/daily/kr/incheon/RKSI

Paris: implied same pattern (Orly/CDG station)

CRITICAL CORRECTION to PHASE1_FOUNDATIONS.md: I assumed METAR/ICAO
direct observation. Reality is more subtle — Polymarket resolves via
WUNDERGROUND's history page for the airport station, NOT raw METAR.
Wunderground pulls from METAR but applies its own QA/smoothing. Two
implications:
  1. We must backtest against Wunderground history values, not raw
     METAR, because that is the actual settlement number.
  2. The airport station is still the right coordinate target (KLGA,
     RKSI) — our forecast grid cell must match the station, not the
     city. The Phase 1 principle holds; only the validation source
     changes from "raw METAR" to "Wunderground station history."

### 2.3 Volume and liquidity
  - Temperature brackets: $483K-$758K per event → real liquidity.
  - Snow brackets: $22K-$1.45M → liquid on storm events, illiquid
    otherwise.
  - Space weather: $9K-$105K → thin, avoid for v1.
  - Per-bucket liquidity unknown without CLOB /book queries; the
    $483K event-level volume is spread across 11 buckets, so a single
    bucket may only have a few thousand dollars of depth. This MUST be
    checked via /book before sizing any real trade.

### 2.4 Settlement prices
All closed markets show outcomePrices of ["0","1"] or ["1","0"] —
binary resolved. The winning bucket = ["1","0"], all others = ["0","1"].
Confirms the $0/$1 payout model and that exactly one bucket wins.

---

## 3. Station mapping — updated from real resolution rules

Extracted station + coordinate mapping from the actual market
descriptions (not guessed):

| City (market label) | Station | ICAO | Lat | Lon | Units |
|---|---|---|---|---|---|
| New York City | LaGuardia Airport | KLGA | 40.777 | -73.873 | °F |
| Seoul | Incheon Intl Airport | RKSI | 37.460 | 126.440 | °C |
| Paris | (Orly/CDG — confirm) | LFPO/LFPG | TBD | TBD | °C |

NOTE: "NYC" resolves to LGA, not JFK and not Central Park. The Phase 1
doc listed KJFK — that is WRONG for these specific markets. The station
is market-specific and must be parsed from the description text, not
assumed from the city name. This is exactly the edge case that loses
money: a model pointed at JFK (cooler, coastal) vs LGA (warmer, inland
Queens) would systematically misprice the 86-87°F bucket.

ACTION: station_lookup must PARSE the resolution description, not just
match city name to a static table. Add a regex step that extracts the
station name and ICAO from the description string.

---

## 4. Assessment — is this data good for a SYSTEM?

YES, with caveats. Score: 7/10 for system viability.

Strengths:
  + Real liquidity ($483K-$1.45M on the temperature/snow markets).
  + Clean bracket structure, mathematically tractable.
  + Resolution rules are explicit and station-anchored.
  + Free forecast data (Open-Meteo) covers all these stations.
  + The edge is real: retail traders price daily city max temp with
    gut feel; an ensemble forecast at the correct station coordinate
    has genuine skill at 1-7 day lead.

Caveats that shape the build:
  - Markets are EPISODIC, not continuous. Zero active at ingest. The
    system must be event-driven: poll Gamma for new weather events,
    trade while open, go quiet between. Do NOT design for always-on.
  - Markets close 1-3 days before the target date (need to confirm
    exact cutoff per event). Edge shrinks as lead time drops because
    the public forecast converges with ours. The sweet spot is 3-7
    days out, where model skill exceeds retail but the market is
    still open.
  - Per-bucket liquidity is the real constraint, not Kelly. We must
    query /book and size to the available ask depth, not the bankroll
    cap. Add a "max shares = top-3 ask depth" rule.
  - Wunderground (not raw METAR) is the settlement oracle. Backtest
    against Wunderground history, which is scrapeable but rate-limited.
  - International markets (Seoul, Paris) use Celsius and non-US
    stations. Open-Meteo handles these fine, but the station table
    must support ICAO codes globally, not just US.

Verdict: build it, but design for episodic event discovery and
liquidity-aware sizing. The edge is real; the discipline is in the
operational details.

---

## 5. Assessment — is this data good for a TUTORIAL?

YES, excellent tutorial material. Score: 9/10 for teaching.

Why it teaches well:
  + The markets are concrete and intuitive ("will NYC hit 90°F on
    April 16?"). Students grok the domain instantly, unlike options
    Greeks or fixed-income math.
  + The full stack is exercised: REST APIs, JSON double-encoding,
    probabilistic reasoning, Kelly sizing, on-chain auth, risk caps.
    No other single project covers this breadth naturally.
  + Real resolution data exists — students can backtest a strategy
    on CLOSED markets and see if their model would have picked the
    right bucket. This is the "lab" component.
  + The edge-case traps (wrong station, inclusive/exclusive bounds,
    thin books, double-encoded fields) are REAL, not invented. Every
    one is a lesson that transfers to other markets.

Tutorial adjustments needed because of the data:
  - Because there are zero active markets right now, the tutorial
    must use CLOSED markets for the backtest/lab section and
    structure the live-trading section as "when a market appears,
    here is the pipeline." Frame it as a dormant system that
    activates on events, not a always-running bot.
  - Use the NYC April 16 market as the canonical worked example —
    we have full data (11 buckets, KLGA station, resolution). It is
    perfect for the EV/Kelly walkthrough. Replace the hypothetical
    85-90°F example in PHASE1_FOUNDATIONS.md with the real
    86-87°F bucket that actually won.
  - Add a "lab" where students pull the closed market, pull the
    Open-Meteo forecast that would have been available 5 days prior,
    compute p_model per bucket, and check whether the strategy would
    have bet on the winning 86-87°F bucket. This is a complete
    end-to-end exercise on real data.

---

## 6. Concrete updates to the project plan

Based on this ingest, the Phase 1 doc needs these corrections before
we code:

1. STATION LOOKUP — parse ICAO from the market description, do not
   hardcode city->station. Add Wunderground URL parsing.

2. RESOLUTION ORACLE — validate against Wunderground station history,
   not raw METAR. Add a wunderground_history client (scrape or
   API-equivalent).

3. EVENT DISCOVERY — the system is event-driven. Add a Gamma poller
   that detects NEW weather events (temperature/snow keywords) and
   queues them. Do not assume continuous markets.

4. LIQUIDITY-AWARE SIZING — add a step that queries /book for the
   target bucket and caps shares at the top-3 ask depth. Kelly +
   bankroll cap + liquidity cap, in that order, smallest wins.

5. UNITS — support both °F (US) and °C (international) brackets.
   Parse from the market question; convert model output to match.

6. BACKTEST MODULE — add src/backtest/ with closed_market_replayer
   that runs the full strategy on past NYC/Seoul/Paris events and
   reports hit rate + simulated PnL. This doubles as the tutorial lab.

7. WORKED EXAMPLE — rewrite the Phase 1 EV/Kelly example using the
   real NYC April 16 market (86-87°F winning bucket at KLGA).

Updated module map:
  src/
    data/
      gamma_client.py          ← + event discovery / new-market poller
      weather_client.py        ← Open-Meteo multi-model, station coords
      station_lookup.py        ← PARSE ICAO from description (not static)
      clob_reader.py           ← /book depth for liquidity caps
      wunderground_history.py  ← NEW: settlement oracle for backtest
    strategy/
      market_mapper.py         ← + °F/°C unit detection
      forecast_prob.py
      ev_engine.py
      kelly.py                 ← + liquidity cap (top-3 ask depth)
    execution/
      clob_trader.py
      order_manager.py
      slippage_guard.py
    risk/
      bankroll.py
      exposure.py
      kill_switch.py
      audit_log.py
    backtest/                  ← NEW
      closed_market_replayer.py
      pnl_report.py
    tests/
      test_station_parse.py    ← extract ICAO from description text
      test_wunderground.py
      test_backtest_nyc_apr16.py  ← lab: would we have picked 86-87°F?

---

## 7. Raw ingested data (reference)

See ingested_markets.json in this folder for the full Gamma response
snapshot of the 22 events. Key markets:

- highest-temperature-in-nyc-on-april-16-2026        KLGA  11 buckets  $483K
- highest-temperature-in-seoul-on-april-17-2026      RKSI  11 buckets  $758K
- highest-temperature-in-paris-on-april-16-2026      TBD   11 buckets  $679K
- how-many-inches-of-snow-in-nyc-this-weekend-jan-24-26  7 buckets  $1.45M
- how-many-inches-of-snow-in-nyc-this-weekend-february-21-23  7 buckets  $22K
- space-weather event counts (5 events, $9K-$105K, thin — v2 only)

End of ingest assessment.