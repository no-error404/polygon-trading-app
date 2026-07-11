# Polymarket Weather Trader — Bot Commands Reference

## Service Control

```bash
# Check if the bot is running
systemctl --user status polymarket-weather-trader.service

# Start the bot
systemctl --user start polymarket-weather-trader.service

# Stop the bot
systemctl --user stop polymarket-weather-trader.service

# Restart the bot
systemctl --user restart polymarket-weather-trader.service

# Check service uptime
ps -o pid,etime,cmd -p $(pgrep -f 'run.py --interval')
```

## Log Files

```bash
# Main log (stdout — may lag due to Python buffering)
tail -n 50 ~/Documents/polymarket-weather-trader/logs/soak_systemd.log

# Live follow the main log
tail -f ~/Documents/polymarket-weather-trader/logs/soak_systemd.log

# Audit log (JSONL — flushes immediately, most reliable)
tail -n 20 ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl

# Count completed cycles
grep -c "Cycle.*complete" ~/Documents/polymarket-weather-trader/logs/soak_systemd.log

# Check for errors
grep -iE "error|traceback|exception|crash" ~/Documents/polymarket-weather-trader/logs/soak_systemd.log | tail -20

# See all dry-run orders generated
grep '"type": "order"' ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl | tail -20

# See forecast data
grep '"type": "forecast"' ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl | tail -10

# See resolved-market guard triggers
grep '"guard"' ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl | tail -10

# See balance checks
grep '"type": "balance"' ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl | tail -5

# Pretty-print last audit entry
tail -n 1 ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl | python3 -m json.tool
```

## Quick Health Check (one-liner)

```bash
echo "SERVICE:"; systemctl --user is-active polymarket-weather-trader.service; echo "CYCLES:"; grep -c "Cycle.*complete" ~/Documents/polymarket-weather-trader/logs/soak_systemd.log; echo "ERRORS:"; grep -ciE "error|traceback|exception" ~/Documents/polymarket-weather-trader/logs/soak_systemd.log; echo "ORDERS:"; grep -c '"type": "order"' ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl; echo "BALANCE:"; grep '"type": "balance"' ~/Documents/polymarket-weather-trader/logs/soak_audit_dryrun.jsonl | tail -1 | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'${d[\"balance_usdc\"]:.2f} USDC, {d[\"open_orders\"]} open orders')"
```

## Cron Monitor Job

```bash
# List cron jobs
hermes cron list

# Run the monitor job manually
hermes cron run a3a57322b6f2

# Remove the monitor job (after 48h soak test done)
hermes cron remove a3a57322b6f2
```

## Key File Locations

```
~/Documents/polymarket-weather-trader/
├── run.py                          # Main bot script
├── config/
│   ├── settings.yaml               # Trading params, discovery queries
│   ├── stations.yaml               # Airport station coordinates
│   └── credentials.yaml            # API keys (gitignored)
├── src/
│   ├── data/gamma_client.py        # Market discovery (Gamma API)
│   ├── data/clob_reader.py         # Orderbook reader (CLOB API)
│   ├── data/weather_client.py      # Multi-model forecasts (Open-Meteo)
│   ├── data/station_lookup.py      # Station matching
│   ├── strategy/market_mapper.py   # Bracket parsing, unit detection
│   ├── strategy/forecast_prob.py   # Probability calculation
│   ├── strategy/ev_engine.py       # EV + resolved-market guard
│   ├── strategy/kelly.py           # Position sizing
│   └── execution/clob_trader.py    # Live order execution (CLOB API)
└── logs/
    ├── soak_systemd.log            # stdout log
    └── soak_audit_dryrun.jsonl     # structured audit trail
```

## Service File

```
~/.config/systemd/user/polymarket-weather-trader.service
```