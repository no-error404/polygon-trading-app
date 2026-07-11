#!/usr/bin/env python3
import sys
from pathlib import Path
from src.risk.pnl_tracker import run_retroactive_analysis

if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent
    log = str(project_root / "logs" / "sniper_audit.jsonl")
    stations = str(project_root / "config" / "stations.yaml")
    
    since = sys.argv[1] if len(sys.argv) > 1 else ""
    run_retroactive_analysis(log, stations, since)
