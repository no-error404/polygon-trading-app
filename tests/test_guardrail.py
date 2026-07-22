"""
Unit tests for GuardRail safety gate.
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.risk.guardrail import GuardRail, LiveTradingBlockedError, live_trading_enabled_for_call


@pytest.fixture
def temp_guard(tmp_path):
    state = tmp_path / "guard.json"
    return GuardRail(state_file=state)


def _hide_credentials(monkeypatch, temp_guard):
    """Make the default credentials path point into the temp dir (no real key)."""
    import src.risk.guardrail as gr
    real_div = type(temp_guard.state_file).__truediv__
    def fake_div(self, other):
        if isinstance(other, str) and other == "config/credentials.yaml":
            return temp_guard.state_file.parent / "no_cred.yaml"
        return real_div(self, other)
    monkeypatch.setattr(gr.Path, "__truediv__", fake_div)


def test_live_blocked_without_key(temp_guard, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    _hide_credentials(monkeypatch, temp_guard)
    with pytest.raises(LiveTradingBlockedError, match="private key|POLYMARKET_PRIVATE_KEY"):
        temp_guard.assert_can_trade_live(dry_run_cycles_required=0, min_orders_required=0)


def test_live_blocked_with_placeholder_key(temp_guard, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "YOUR_PRIVATE_KEY_HERE")
    _hide_credentials(monkeypatch, temp_guard)
    with pytest.raises(LiveTradingBlockedError, match="private key|POLYMARKET_PRIVATE_KEY"):
        temp_guard.assert_can_trade_live(dry_run_cycles_required=0, min_orders_required=0)


def test_live_blocked_until_dry_cycles_met(temp_guard, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    temp_guard.record_dry_run_cycle(orders_generated=5, errors=0)
    with pytest.raises(LiveTradingBlockedError, match="dry-run cycles"):
        temp_guard.assert_can_trade_live(dry_run_cycles_required=100)


def test_live_blocked_if_dry_run_errors(temp_guard, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    for _ in range(100):
        temp_guard.record_dry_run_cycle(orders_generated=2, errors=0)
    temp_guard.record_dry_run_cycle(orders_generated=200, errors=1)
    with pytest.raises(LiveTradingBlockedError, match="errors"):
        temp_guard.assert_can_trade_live(max_errors_allowed=0)


def test_live_allowed_when_all_gates_pass(temp_guard, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    for _ in range(100):
        temp_guard.record_dry_run_cycle(orders_generated=2, errors=0)
    temp_guard.assert_can_trade_live(
        dry_run_cycles_required=100,
        min_orders_required=10,
        max_errors_allowed=0,
    )


def test_dry_run_never_counts_as_live(temp_guard, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    _hide_credentials(monkeypatch, temp_guard)
    assert live_trading_enabled_for_call(dry_run=True) is False


def test_live_call_without_key_is_false(temp_guard, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    _hide_credentials(monkeypatch, temp_guard)
    assert live_trading_enabled_for_call(dry_run=False) is False


def test_state_persists(temp_guard, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    temp_guard.record_dry_run_cycle(orders_generated=10, errors=0)
    guard2 = GuardRail(state_file=temp_guard.state_file)
    assert guard2.record.cycles_completed == 1
    assert guard2.record.orders_generated == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
