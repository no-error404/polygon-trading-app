Weather Trading on Polymarket: A Complete Beginner's Step-by-Step Guide
Welcome. This is the most detailed, beginner-friendly guide you will find on building a weather trading bot for Polymarket from absolute zero. No prior experience assumed—just patience and a willingness to learn.

Part 1: Why Weather Markets?
Weather markets are the ideal training ground for a beginner trader. Here's why:

Characteristic	Weather Markets	Politics/Crypto Markets
Data source	Structured, quantifiable, public	Unstructured, narrative-driven
Insider information	Nearly impossible	Common
Resolution	Clear, objective (thermometer)	Often disputed
Frequency	Daily	Weeks/months
Competition	Lower	Fierce
As one trader put it: "No narratives, no insiders — a pure forecasting problem. Perfect for code." Weather markets on Polymarket are "among the most predictable yet under-automated verticals". Temperature ranges, precipitation, and extreme weather events "create repeatable statistical edges".

The core insight: Weather markets ask simple questions like "Will the high temperature in New York City on July 15 be above 85°F?" The market prices YES shares at some probability (e.g., $0.62 = 62%). Your job is to compute your own probability using weather forecast data and trade when the market is wrong.

Part 2: The Mental Model You Must Internalize
Before writing a single line of code, understand this:

You are not predicting the weather. You are predicting what the weather forecast models predict—and comparing that to what the market has priced.

The market price represents the crowd's probability. Your edge comes from being better at interpreting forecast data than the crowd. You don't need to be right 100% of the time. You need to be right more often than the market expects, and size your bets accordingly.

A professional weather trader's realistic results show this clearly: "Chongqing 26°C +1317% · Milan −70% · Munich −71% · Buenos Aires −71%. The strategy isn't 'win every time' — it's that a rare asymmetric win can outweigh several small, capped losses."

Part 3: Prerequisites – What You Need Before Starting
3.1 Hardware
A computer (Windows, Mac, or Linux) for development

(Optional but recommended) A VPS (Virtual Private Server) for 24/7 operation—DigitalOcean's cheapest $6/month droplet is sufficient

3.2 Software
Python 3.9 or higher (3.12 recommended)

A code editor (VS Code is free and excellent)

Git (for cloning repositories)

A Telegram account (for bot notifications)

3.3 Accounts
Polymarket account – Create one at polymarket.com using email (simpler than wallet method for beginners)

Polygon wallet – You'll need a wallet like MetaMask. Fund it with a small amount of USDC on the Polygon network—start with $50–$100, never more

Weather API key(s) – Sign up for free at:

Open-Meteo – No API key required, completely free

Visual Crossing – 1,000 free records per day

OpenWeatherMap – 1,000 free calls per day

3.4 Mindset
Start in simulation mode. Never go live on day one.

Accept that you will lose money on some trades. This is normal.

Think in probabilities, not certainties.

Part 4: Phase 0 – Understanding the Polymarket CLOB (Central Limit Order Book)
Polymarket uses a CLOB (Central Limit Order Book) for trading. Think of it like a stock exchange:

Orders are matched off-chain (fast, cheap)

Settlement happens on-chain (Polygon blockchain, USDC)

Bid = the highest price someone is willing to pay for YES

Ask = the lowest price someone is willing to sell YES for

Spread = Ask - Bid (your cost of trading)

The official Python client is py-clob-client-v2. Installation is simple:

bash
pip install py_clob_client_v2
Authentication requires two levels:

L1: Wallet signature (EIP-712) to create API keys

L2: HMAC with API credentials for placing orders

Part 5: Phase 1 – Picking Your First Market
5.1 Find Weather Markets on Polymarket
Go to Polymarket → Weather category. As of 2026, there are 201 weather markets listed. Look for:

Daily temperature markets – "Will the high temperature in [City] be above X°F on [Date]?"

Markets with sufficient liquidity – At least $1,000–$5,000 in trading volume

Cities with reliable forecast data – NYC, Chicago, Miami, London, Tokyo

5.2 Choose ONE City to Start
Do not trade multiple cities initially. Pick ONE:

New York City – Excellent data availability, active market

Chicago – Good data, slightly less competition

Miami – Warm bias to correct for (more on this later)

5.3 Understand the Market Structure
A temperature market typically has multiple "brackets":

"Temperature ≤ 75°F"

"Temperature 76–80°F"

"Temperature 81–85°F"

"Temperature ≥ 86°F"

Each bracket is a separate YES/NO contract. Only one bracket will resolve to YES (the one containing the actual temperature). All others resolve to NO.

5.4 Manual Practice (Week 1 – No Code)
Before automating anything:

Pick a market that resolves in 2–3 days

Write down your probability estimate for each bracket

Compare to the market prices

Wait for resolution

See how you did

Do this for 10–20 markets manually. This builds intuition and costs nothing.

Part 6: Phase 2 – Setting Up Your Development Environment
6.1 Create a Project Folder
bash
mkdir polymarket-weather-bot
cd polymarket-weather-bot
6.2 Set Up a Python Virtual Environment
bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
6.3 Install Required Packages
bash
pip install py_clob_client_v2 python-dotenv requests web3
6.4 Create Your .env File
This stores your secrets (never commit this to GitHub!):

bash
# .env
POLYGON_PRIVATE_KEY=your_private_key_here
CLOB_API_KEY=your_api_key_here
CLOB_SECRET=your_api_secret_here
CLOB_PASS_PHRASE=your_passphrase_here
VISUAL_CROSSING_API_KEY=your_key_here  # Optional
6.5 Get Your CLOB API Credentials
python
# get_creds.py
import os
from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient

load_dotenv()

host = "https://clob.polymarket.com"
chain_id = 137  # Polygon mainnet

client = ClobClient(
    host=host,
    chain_id=chain_id,
    key=os.environ["POLYGON_PRIVATE_KEY"]
)

creds = client.create_or_derive_api_key()
print(f"API Key: {creds.api_key}")
print(f"Secret: {creds.api_secret}")
print(f"Passphrase: {creds.api_passphrase}")
Run this once, copy the credentials to your .env file.

Part 7: Phase 3 – Getting Weather Data (The Core Signal)
7.1 Option A: Open-Meteo (Easiest, No API Key)
Open-Meteo provides free ensemble forecasts:

python
# weather_data.py
import requests
import json

def get_forecast(lat, lon, date):
    """Get ensemble forecast for a specific date"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": "America/New_York",
        "start_date": date,
        "end_date": date,
        "models": "ecmwf_ifs,ecmwf_aifs,ncep_gfs,icon"  # Ensemble members
    }
    response = requests.get(url, params=params)
    return response.json()

# NYC coordinates
nyc_lat, nyc_lon = 40.7128, -74.0060
forecast = get_forecast(nyc_lat, nyc_lon, "2026-07-20")
print(forecast)
7.2 Option B: Visual Crossing (More Detailed, 1,000 Free Records/Day)
python
def get_visual_crossing_forecast(location, date):
    api_key = os.environ["VISUAL_CROSSING_API_KEY"]
    url = f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{location}/{date}?key={api_key}"
    response = requests.get(url)
    return response.json()
7.3 Option C: National Weather Service API (US Only, Authoritative)
The NWS API is a powerful tiebreaker. As one experienced trader notes: "When the ensemble disagrees with market pricing, the National Weather Service forecast is a powerful tiebreaker. NWS human forecasters often catch local effects the ensemble misses."

python
def get_nws_forecast(lat, lon):
    # Get forecast office and grid
    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    points = requests.get(points_url).json()
    grid_id = points["properties"]["gridId"]
    grid_x = points["properties"]["gridX"]
    grid_y = points["properties"]["gridY"]
    
    # Get forecast
    forecast_url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast"
    forecast = requests.get(forecast_url).json()
    return forecast
7.4 Building a Simple Ensemble
The best practice is to combine multiple sources and apply city-specific bias corrections:

City	Bias	Notes
NYC	+1.4°F warm	Ensemble runs warm vs reality
MIA	−2.2°F cool	Ensemble runs cool vs reality
CHI	+1.4°F warm	Assumed NYC-equivalent
Example bias correction:

python
def apply_bias_correction(forecast_temp, city):
    biases = {
        "NYC": -1.4,   # Subtract because ensemble runs warm
        "MIA": +2.2,   # Add because ensemble runs cool
        "CHI": -1.4,
        "LAX": -1.4
    }
    return forecast_temp + biases.get(city, 0)
Part 8: Phase 4 – The Trading Logic (How to Decide When to Trade)
8.1 The Core Formula
text
Edge = Model_Probability - Market_Price
Positive edge → Market underprices YES → Buy YES

Negative edge → Market overprices YES → Buy NO

8.2 Calculating Model Probability
For a temperature market with brackets, you need to convert your forecast into a probability distribution:

python
def calculate_bracket_probabilities(forecast_temp, uncertainty=3):
    """
    Convert a point forecast into bracket probabilities.
    uncertainty = standard deviation of forecast error (typically 2-4°F)
    """
    import math
    
    brackets = [75, 80, 85, 90]  # Example thresholds
    probs = {}
    
    for i, threshold in enumerate(brackets):
        if i == 0:
            # Probability temp <= first threshold
            z = (threshold - forecast_temp) / uncertainty
            prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        else:
            # Probability between thresholds
            z_high = (threshold - forecast_temp) / uncertainty
            z_low = (brackets[i-1] - forecast_temp) / uncertainty
            prob = 0.5 * (1 + math.erf(z_high / math.sqrt(2))) - \
                   0.5 * (1 + math.erf(z_low / math.sqrt(2)))
        probs[f"≤{threshold}"] = prob
    
    # Probability > last threshold
    z = (brackets[-1] - forecast_temp) / uncertainty
    probs[f">{brackets[-1]}"] = 1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))
    
    return probs
8.3 The Signal Threshold
Don't trade on tiny edges. Transaction costs (spread + fees) will eat them. Use minimum edge thresholds:

NO signals: Require ≥ 5 percentage points edge

YES signals: Require ≥ 15 percentage points edge

This asymmetry exists because YES positions are generally riskier (binary outcome vs. multiple NO brackets).

8.4 The "Stay Away from the Middle" Rule
Markets near the ensemble mean are high-variance bets. Use a 3°F buffer around the corrected mean—if your forecast falls within 3°F of the bracket boundary, skip the trade.

Part 9: Phase 5 – Position Sizing (The Kelly Criterion)
This is the most important risk management tool you will learn.

9.1 The Kelly Formula
text
f* = (Edge / Odds)
Where:

Edge = Your probability advantage (Model_Prob - Market_Price)

Odds = Decimal odds offered by the market (1 / Market_Price)

Example:

Market price for YES = $0.60 (60% implied probability)

Your model says probability = 70%

Edge = 10% (0.10)

Odds = 1 / 0.60 = 1.67

Kelly fraction = 0.10 / 1.67 = 0.06 (6% of bankroll)

9.2 Quarter-Kelly (Conservative Approach)
Professional traders rarely bet full Kelly. Use Quarter-Kelly:

text
Position Size = Bankroll × (Kelly_Fraction / 4)
This reduces volatility while still capturing most of the edge.

9.3 Hard Caps (Non-Negotiable)
Set these rules and never break them:

Max 2% of bankroll per trade (even if Kelly says more)

Daily loss limit: 5% of bankroll → if hit, stop trading for the day

Max 5 open positions at any time

Part 10: Phase 6 – The Complete Bot (Putting It All Together)
10.1 The Bot Structure
text
polymarket-weather-bot/
├── .env                 # Secrets
├── config.json          # Settings
├── weather_data.py      # Fetches forecasts
├── market_data.py       # Fetches Polymarket prices
├── trading_logic.py     # Calculates edges and signals
├── execution.py         # Places orders
├── bot.py               # Main loop
├── verify.py            # Checks trade outcomes
└── signals.csv          # Trade log
10.2 config.json
json
{
    "cities": ["NYC"],
    "min_edge_yes": 0.15,
    "min_edge_no": 0.05,
    "max_position_pct": 0.02,
    "daily_loss_limit": 0.05,
    "kelly_fraction": 0.25,
    "scan_interval_minutes": 60,
    "forecast_horizon_days": 3
}
10.3 market_data.py – Fetch Polymarket Prices
python
import os
from py_clob_client_v2 import ClobClient, ApiCreds

def get_market_price(token_id):
    """Get current market price for a token"""
    host = "https://clob.polymarket.com"
    chain_id = 137
    
    creds = ApiCreds(
        api_key=os.environ["CLOB_API_KEY"],
        api_secret=os.environ["CLOB_SECRET"],
        api_passphrase=os.environ["CLOB_PASS_PHRASE"]
    )
    
    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=os.environ["POLYGON_PRIVATE_KEY"],
        creds=creds
    )
    
    # Get order book for the token
    book = client.get_order_book(token_id)
    # Mid-price approximation
    if book['bids'] and book['asks']:
        best_bid = float(book['bids'][0]['price'])
        best_ask = float(book['asks'][0]['price'])
        return (best_bid + best_ask) / 2
    return None
10.4 trading_logic.py – Signal Generation
python
def generate_signals(forecast_temp, market_prices, city):
    """Generate trading signals based on edge calculation"""
    signals = []
    
    # Calculate model probabilities
    model_probs = calculate_bracket_probabilities(forecast_temp)
    
    for bracket, model_prob in model_probs.items():
        market_price = market_prices.get(bracket)
        if market_price is None:
            continue
            
        edge = model_prob - market_price
        
        if edge > config["min_edge_yes"]:
            signals.append({
                "bracket": bracket,
                "side": "BUY_YES",
                "edge": edge,
                "size": calculate_kelly_size(edge, market_price)
            })
        elif edge < -config["min_edge_no"]:
            signals.append({
                "bracket": bracket,
                "side": "BUY_NO",
                "edge": -edge,
                "size": calculate_kelly_size(-edge, 1 - market_price)
            })
    
    return signals
10.5 execution.py – Placing Orders
python
def place_order(token_id, side, price, size):
    """Place a limit order on Polymarket"""
    from py_clob_client_v2 import OrderArgs, OrderType, Side
    
    client = get_authenticated_client()
    
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        side=Side.BUY if side == "BUY_YES" else Side.SELL,
        size=size
    )
    
    response = client.create_and_post_order(
        order_args=order_args,
        order_type=OrderType.GTC,  # Good 'til cancelled
        options={"tick_size": "0.01"}
    )
    
    return response
10.6 bot.py – The Main Loop
python
import time
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)

def main_loop():
    """Main trading loop - runs every 60 minutes"""
    while True:
        try:
            logging.info(f"Scan starting at {datetime.now()}")
            
            for city in config["cities"]:
                # 1. Get forecast
                forecast = get_forecast(city["lat"], city["lon"], tomorrow())
                
                # 2. Apply bias correction
                corrected_temp = apply_bias_correction(forecast, city["name"])
                
                # 3. Get market prices
                market_prices = get_all_bracket_prices(city["market_ids"])
                
                # 4. Generate signals
                signals = generate_signals(corrected_temp, market_prices, city["name"])
                
                # 5. Execute trades
                for signal in signals:
                    if signal["size"] > 0:
                        place_order(
                            token_id=signal["token_id"],
                            side=signal["side"],
                            price=signal["price"],
                            size=signal["size"]
                        )
                        log_trade(signal)
            
            # 6. Check daily loss limit
            if daily_loss() > config["daily_loss_limit"]:
                logging.warning("Daily loss limit hit. Halting until tomorrow.")
                break
                
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
        
        # Wait 60 minutes
        time.sleep(3600)
Part 11: Phase 7 – Verification and Backtesting
11.1 Verify Your Trades
After markets resolve, check if you were right:

python
# verify.py
def verify_trades():
    """Check resolved trades against actual weather data"""
    trades = load_trades("signals.csv")
    
    for trade in trades:
        if trade["status"] == "RESOLVED":
            actual_temp = get_actual_temperature(trade["city"], trade["date"])
            was_correct = (actual_temp in trade["bracket"])
            trade["outcome"] = "WIN" if was_correct else "LOSS"
            trade["pnl"] = trade["size"] * (1 if was_correct else 0) - trade["cost"]
    
    save_trades(trades)
    return calculate_performance(trades)
11.2 Use Existing Open-Source Bots as Reference
Several excellent open-source bots exist. Study them before building your own:

polymarket-weather-bot by natestokens – 173-member ensemble, bias corrections, no API keys required

polymarket-kalshi-weather-bot – Multi-platform, Kelly sizing, GFS ensemble

polymarket_temperature – Full historical backtesting environment

11.3 The Hermes Agent Approach (Advanced)
If you want to go further, Hermes Agent provides a self-learning framework:

"Hermes stands out due to three architectural strengths: Persistent Memory, Self-Improving Skills, and Always-On Execution. This creates a true closed learning loop: the agent gets measurably better at your specific weather trading workflow over time."

Deploy Hermes on a VPS:

bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
hermes gateway setup  # Connect to Telegram
hermes  # Start interactive session
Then initialize with the weather bot prompt.

Part 12: Phase 8 – Going Live (The Right Way)
12.1 The Gradual On-Ramp
Week	Activity	Capital
Week 1-2	Manual paper trading	$0
Week 3-4	Bot in simulation mode	$0
Week 5	Bot with $SIM virtual currency	$0
Week 6	Live with $50, ONE city	$50
Week 7+	Scale gradually	$50 → $500
12.2 Safety Rails (Copy These)
The JinDaGe weather trader skill has excellent safety defaults:

Dry-run is the default – No trades execute without --live flag

Per-trade cap – Default $2.00 per trade

Daily caps – Max trades/day, max USD/day

Auto stop-loss – Server-side risk monitoring

12.3 What to Monitor Daily
PnL – Total profit/loss

Win rate – Percentage of winning trades

Average win vs. average loss – Is your strategy profitable?

Drawdown – Maximum peak-to-trough decline

Edge distribution – Are you trading only quality signals?

12.4 Realistic Expectations
With disciplined sizing and multiple cities:

Starting capital: $100–$500

Conservative monthly target: 40–100%+ (highly variable)

Key success factor: Consistent execution + letting the strategy compound

Part 13: Common Mistakes and How to Avoid Them
Mistake 1: Trading Without an Edge
"Automation only amplifies an existing edge; with no edge you just lose faster and pay more in fees."

Fix: Backtest extensively before going live. If you can't prove an edge in simulation, you won't have one live.

Mistake 2: Over-trading
Fix: Set a minimum edge threshold. Skip marginal opportunities. Quality > quantity.

Mistake 3: Ignoring the Spread
The bid-ask spread is a real cost. If the spread is 2 cents and your edge is 3 cents, you're barely profitable.

Fix: Only trade markets with tight spreads (< 1 cent).

Mistake 4: Trading Crowded Markets
"The mispricings live in low-attention markets, not the crowded headline ones."

Fix: Look for secondary cities, less obvious weather questions.

Mistake 5: Not Verifying Trades
Fix: Always verify your trades against actual weather data. Learn from every trade.

Part 14: Your 90-Day Roadmap
Days 1-7: Learning
Read this guide twice

Explore Polymarket weather markets manually

Set up your development environment

Get API keys

Days 8-21: Building
Write the weather data fetcher

Write the market data fetcher

Implement the trading logic

Test each component individually

Days 22-35: Simulation
Run the bot in paper trading mode

Collect 50+ simulated trades

Analyze performance

Refine parameters

Days 36-49: $SIM Testing
Use Simmer's $SIM virtual currency

Trade at real prices with fake money

Validate your edge

Days 50-60: Live Pilot
Start with $50 in ONE city

Run for 10 days

Review every trade

Days 61-90: Scaling
If profitable, add a second city

Gradually increase position size

Consider adding Hermes Agent for self-learning

Part 15: Final Words of Wisdom
Weather markets on Polymarket "remain one of the cleanest edges on Polymarket because they combine: structured, quantifiable data (forecast APIs), clear resolution windows, and lower narrative noise than politics or crypto".

You have a real opportunity here. But it requires:

Discipline (follow your rules)

Patience (don't force trades)

Continuous learning (review every trade)

Start small. Learn constantly. Scale gradually.

"The strategy isn't 'win every time' — it's that a rare asymmetric win can outweigh several small, capped losses."

Go build your bot. And remember: never trade money you cannot afford to lose.

