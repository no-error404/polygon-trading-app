#!/bin/bash
cd /home/rory/polymarket-trader
echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
echo "Service: $(systemctl --user is-active polymarket-weather-sniper.service)"
echo "Cycles: $(grep -c '"type": "cycle"' logs/sniper_audit.jsonl)"
echo "Orders: $(grep -c '"type": "order"' logs/sniper_audit.jsonl)"
echo "Last 3 audit lines:"
tail -n 3 logs/sniper_audit.jsonl
echo "Recent errors:"
journalctl --user -u polymarket-weather-sniper.service --since "6 hours ago" --no-pager 2>/dev/null | grep -iE "error|traceback|critical|warn" | tail -5
echo "---"