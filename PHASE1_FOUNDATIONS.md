# Phase 1 — Foundations: Polymarket Weather Trading

Mentor doc. Read this before we write a single line of trading code.
Concise, practical, edge-case focused.

---

## 1. POLYMARKET MECHANICS

### 1.1 Two APIs, two jobs — never mix them

Polymarket exposes two distinct HTTP surfaces. The single most common
newbie mistake is calling the wrong one.

| | Gamma API | CLOB API |
|---|---|---|
| Host | `gamma-api.polymarket.com` | `clob.polymarket.com` |
| Auth | None. Public read-only. | EIP-712 wallet signatures (Polygon) |
| Use for | Discovering markets, searching, browsing | Real-time prices, orderbook, placing/cancelling orders |
| Rate limit | ~4,000 req / 10s | ~9,000 req / 10s |
| Python client | `requests` / `httpx` — no SDK needed | `py-clob-client` (official, authenticated) |

RULE: Discovery = Gamma. Execution = CLOB. We keep them in separate
Python modules so an auth-key bug can never leak into a read-only call
and so read-only code can run safely without a wallet.

### 1.2 Conditional tokens on Polygon

Every Polymarket market is an on-chain Conditional Token Framework (CTF)
contract on Polygon. A market has a `conditionId` (a 32-byte hash of
the oracle question + outcomes). The CTF mints two ERC-1155 token IDs
derived from that condition:

  - YES token  (clobTokenIds[0])
  - NO token   (clobTokenIds[1])

Each token pays out $1.00 USDC if its outcome is correct at resolution,
$0.00 otherwise. The market price (0.00–1.00) IS the implied probability
because payout is binary $0/$1.

CRITICAL EDGE CASE: `outcomePrices`, `outcomes`, and `clobTokenIds` are
returned from Gamma as JSON *strings inside JSON* (double-encoded). You
MUST `json.loads(market['clobTokenIds'])` before using a token ID, or
your CLOB calls 404 silently. This bites everyone the first time.

### 1.3 Negative-risk / multi-outcome bracket markets

Weather markets are rarely binary "Will it be hot? Yes/No". They are
BRACKETS: e.g. "Max temp in NYC on July 15" with outcomes:

  [<85°F] [85-90] [90-95] [95-100] [>=100°F]

These are linked markets under one **Event**. Polymarket groups them with
a "Negative Risk" mechanism so that buying one bracket and selling
another settles at $1 only for the winning bucket — the shares are
mutually exclusive and exhaustive.

Why this matters for us:
  - We treat the bracket as a CATEGORICAL distribution, not 5 independent
    binary markets. P(all outcomes) must sum to 1.00.
  - If the market's implied probabilities sum to 1.10 (overround) or
    0.92 (underround), that spread itself is tradeable info.
  - We pick the bracket whose model probability diverges most from the
    implied price, after normalising the book.

---

## 2. WEATHER MODEL ARBITRAGE

### 2.1 The thesis

Retail traders price weather with gut feel + a glance at the Weather
Channel. We price it with multi-model ensemble forecasts and a
calibration layer. The edge lives in the gap between:
  - implied probability p_market (from CLOB price)
  - model probability   p_model  (from forecast ensembles)

When |p_model - p_market| is large AND the model is well-calibrated, we
have positive expected value.

### 2.2 Free, no-auth data sources

| Source | What | Auth | Endpoint pattern |
|---|---|---|---|
| Open-Meteo | Ensemble temps, all models (GFS, ECMWF, ICON, MET Norway) | None | `api.open-meteo.com/v1/forecast` + `models=gfs_seamless,ecmwf,...` |
| NOAA HRRR | Hourly hi-res over CONUS, best for 0-48h | None | `https://nomads.ncep.noaa.gov/dods/hrrr/hrrr...` (OPeNDAP) |
| NOAA GFS | Global, 0.25°, out to 384h | None | `nomads.ncep.noaa.gov/dods/gfs_0p25` |
| IEM / Iowa ASOS | METAR archive + live for US airports | None | `mesonet.agron.iastate.edu/json/...` |

We start with Open-Meteo because one call returns multiple model
ensembles in JSON — no OPeNDAP, no grib decoding. NOAA NOMADS is the
fallback for HRRR-specific resolution.

### 2.3 Why airport stations (METAR / ICAO) are non-negotiable

Polymarket weather markets resolve against a NAMED STATION, almost
always an airport METAR site:
  - "NYC (Central Park)" → KNYC feed
  - "Chicago O'Hare"     → KORD
  - "Denver"             → KDEN
  - "Los Angeles (LAX)"  → KLAX

METAR is the official observation of record. If you model "LA
temperature" using a downtown open-meteo grid cell and the market
resolves against KLAX (coastal, marine-cooled), your model is
systematically biased hot — you will lose money on every sea-breeze
day.

RULE: Map every market to its ICAO station FIRST, then pull the
forecast grid cell whose lat/lon is the station's official coordinates,
not the city centroid. We maintain a station lookup table:

  KORD  41.960  -87.910  Chicago O'Hare
  KJFK  40.640  -73.780  New York JFK
  KDEN  39.856 -104.847  Denver Intl
  KLAX  33.938 -118.389  Los Angeles Intl

### 2.4 Multi-model consensus vs edge

We pull 4 models for every target day:
  1. HRRR (hi-res, short lead, US only)
  2. GFS  (global, medium range)
  3. ECMWF (global, best skill at 3-7d)
  4. NBM / National Blend (consensus already, good baseline)

Two regimes:
  - **Consensus trade**: all 4 models agree on a bracket with >60%
    probability but market prices it at <40%. High conviction, size up
    (within Kelly).
  - **Spread / disagreement trade**: models disagree wildly → market
    is pricing uncertainty fairly → trade only if a single model we
    trust more (ECMWF at 5d) leans hard one way.

We compute p_model as the ensemble fraction of runs landing in each
bracket, with a calibration shrinkage toward climatology for small
samples.

---

## 3. QUANTITATIVE MATH

### 3.1 Expected Value on a temperature bracket

Market: "NYC max temp July 15" bracket [85–90°F].
CLOB YES price = 0.30  →  p_market = 0.30  →  cost = $0.30/share.

Our 4-model ensemble: 90 of 120 runs land in 85–90°F
  →  p_model = 90/120 = 0.75.

EV per $1-share of YES:
  EV = p_model * (1.00 - cost) - (1 - p_model) * cost
     = 0.75 * 0.70  -  0.25 * 0.30
     = 0.525 - 0.075
     = +$0.45/share

Positive and large. Compare with NO side (price 0.70):
  EV_NO = (1-0.75)*(1-0.70) - 0.75*0.70 = 0.075 - 0.525 = -$0.45
Confirms symmetry — only one side has edge.

EDGE CASES:
  - Always EV-filter the side you actually can buy at the ASK, not the
    midpoint. Slippage matters.
  - Bracket boundary: 85.0°F — is it inclusive? Read the market rules
    text. "85 or above" vs "above 85" flips a bucket. We store the
    exact boundary spec per market.
  - If p_model is from a small ensemble (<30 runs), shrink toward 1/N
    (uniform) to avoid overbetting noise.

### 3.2 Fractional Kelly position sizing

Full Kelly on a binary bet:
  f* = (b*p - q) / b
where:
  p = p_model (your edge probability)
  q = 1 - p
  b = net odds = (1 - price) / price    [you win $1-price per $1 risked? 
                                          NO — see below]

Cleaner binary Kelly form (correct for $0/$1 payout shares):
  f* = p - (1-p) * (price / (1 - price))

Example with p_model = 0.75, price = 0.30:
  f* = 0.75 - 0.25 * (0.30/0.70)
     = 0.75 - 0.107
     = 0.643  → 64.3% of bankroll on this one bet

64% is INSANE. Full Kelly maximises long-run growth but has ~33%
drawdowns and assumes your p_model is perfectly calibrated (it isn't).

FRACTIONAL KELLY: we use f = 0.25 * f* (quarter-Kelly) as default.
  → f = 0.25 * 0.643 = 0.161 → 16.1% of bankroll.

Quarter-Kelly:
  - Gives up only ~25% of long-run growth rate
  - Cuts drawdown variance dramatically
  - Survives miscalibrated models (the real killer)

HARD RULES (Risk Management module enforces these):
  - Cap any single market at 5% of bankroll regardless of Kelly output.
  - Cap total open weather exposure at 25% of bankroll.
  - Skip trade if EV < $0.03/share (noise threshold).
  - Skip trade if f* <= 0 (no edge).
  - Recompute p_model every forecast cycle; auto-close if edge flips
    negative and we can exit at mid.

### 3.3 Worked sizing example

  Bankroll:        $1,000
  p_model:         0.75
  price (ask):     0.31   (use ask, not mid, for buys)
  f* = 0.75 - 0.25*(0.31/0.69) = 0.75 - 0.1123 = 0.6377
  f  = 0.25 * 0.6377 = 0.1594
  Kelly $ = 0.1594 * 1000 = $159.40
  Single-market cap = 5% * 1000 = $50   ← BINDING
  Final bet = $50  →  shares = 50 / 0.31 = 161 shares
  Max loss = $50.  Max payout = 161 * $1 = $161  → profit $111.

The cap binds before Kelly — that is the point. We never let a single
hot forecast vaporise the bankroll.

---

## 4. PYTHON PROJECT BLUEPRINT (4 stages)

Project root:  /home/rory/Documents/polymarket-weather-trader

```
polymarket-weather-trader/
├── PHASE1_FOUNDATIONS.md          ← this file
├── requirements.txt
├── config/
│   ├── stations.yaml              ← ICAO -> lat/lon, market slug mapping
│   └── settings.yaml              ← bankroll, kelly_fraction, caps, API hosts
├── src/
│   ├── __init__.py
│   ├── data/                      ← STAGE 1
│   │   ├── __init__.py
│   │   ├── gamma_client.py        ← read-only market discovery (Gamma)
│   │   ├── weather_client.py      ← Open-Meteo + NOAA fetch, multi-model
│   │   ├── station_lookup.py      ← ICAO -> coords, market -> station
│   │   └── clob_reader.py         ← read-only prices/orderbook (CLOB GET)
│   ├── strategy/                  ← STAGE 2
│   │   ├── __init__.py
│   │   ├── market_mapper.py       ← parse bracket markets, normalise book
│   │   ├── forecast_prob.py       ← ensemble -> bracket probabilities
│   │   ├── ev_engine.py           ← EV calc per bracket, edge filter
│   │   └── kelly.py               ← fractional Kelly sizing w/ caps
│   ├── execution/                 ← STAGE 3
│   │   ├── __init__.py
│   │   ├── clob_trader.py         ← py-clob-client wrapper (auth, sign)
│   │   ├── order_manager.py       ← limit orders, GTC/GTD, retry logic
│   │   └── slippage_guard.py      ← ask-vs-mid checks, max-slip rules
│   └── risk/                      ← STAGE 4
│       ├── __init__.py
│       ├── bankroll.py            ← track balance, realised PnL
│       ├── exposure.py            ← aggregate open positions, enforce caps
│       ├── kill_switch.py         ← halt trading on drawdown / API error
│       └── audit_log.py           ← every decision + every order, append-only
├── tests/
│   ├── test_ev.py
│   ├── test_kelly.py
│   ├── test_station_lookup.py
│   └── fixtures/                  ← cached API responses for offline tests
└── run.py                         ← orchestrator: ingest -> strategy -> risk -> exec
```

### Stage 1 — Data Ingestion (read-only, no auth)
  Inputs:  Gamma search query, station table, forecast lead days.
  Outputs: normalised market objects, forecast ensembles, live orderbooks.
  Modules: `gamma_client`, `weather_client`, `station_lookup`, `clob_reader`.
  Edge cases to handle NOW:
    - double-encoded JSON fields from Gamma (json.loads twice where needed)
    - market with empty clobTokenIds → skip, log
    - Open-Meteo model field variance → fall back to gfs_seamless
    - station not in table → raise, never guess a city centroid

### Stage 2 — Strategy Engine (pure functions, no I/O)
  Inputs:  markets + forecasts + orderbooks (from Stage 1).
  Outputs: signed opportunities {market, side, p_model, price, EV, kelly_f, size}.
  Modules: `market_mapper`, `forecast_prob`, `ev_engine`, `kelly`.
  Edge cases:
    - bracket boundaries inclusive/exclusive → parse rules text, flag ambiguity
    - ensemble < 30 runs → shrink toward uniform
    - book overround/underround → normalise before comparing
    - p_model numerically 0 or 1 → clip to [0.01, 0.99] to avoid div-by-zero in Kelly

### Stage 3 — Order Execution (auth, Polygon, py-clob-client)
  Inputs:  approved opportunities from Stage 2 + risk approval.
  Outputs: filled/cancelled order receipts.
  Modules: `clob_trader`, `order_manager`, `slippage_guard`.
  Edge cases (THIS IS WHERE MONEY IS LOST):
    - always POST limit orders, NEVER market orders on thin weather books
    - tick size 0.01 — price must align or order is rejected
    - min order size from /book response — enforce or reject
    - sign-and-submit retry with backoff on Polygon congestion
    - never reuse a nonce; track nonce monotonic
    - GTC orders need explicit cancel if forecast flips — set GTD with
      horizon = next forecast cycle

### Stage 4 — Risk Management (runs BEFORE and AFTER Stage 3)
  Pre-trade:  bankroll check, single-market cap, aggregate exposure cap,
              kill-switch state.
  Post-trade: PnL accrual, drawdown monitor, audit log append.
  Modules: `bankroll`, `exposure`, `kill_switch`, `audit_log`.
  Edge cases:
    - bankroll read from on-chain USDC balance, not a local number —
      local can drift; reconcile every cycle
    - kill-switch triggers: drawdown > 8% in 24h, 3 consecutive rejected
      orders, Gamma/CLOB unreachable for >2 cycles
    - audit log is append-only JSONL, never overwrite — it is your
      forensic record when (not if) something goes wrong

---

## Build order (what we code next)

  1. `config/stations.yaml` + `station_lookup.py`  — the foundation, no I/O
  2. `gamma_client.py` + tests against a live weather market
  3. `weather_client.py` for one station (e.g. KJFK), multi-model
  4. `ev_engine.py` + `kelly.py` with unit tests on the worked examples above
  5. THEN and only then: `clob_trader.py` with paper-trading keys

We do NOT touch authenticated CLOB code until Stages 1-2 are tested and
producing positive-EV signals on paper. Order of operations is safety-
first: lose money in simulation, not on Polygon.

---

End of Phase 1. Reply "go phase 2" to start coding Stage 1 modules.