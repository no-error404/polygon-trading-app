import pytest
import os
from pathlib import Path
from src.execution.clob_trader import ClobTrader, AuthError, OrderState, PUSD_CONTRACT
from src.execution.slippage_guard import SlippageGuard
from src.risk.bankroll import Bankroll
from src.risk.exposure import ExposureManager
from src.risk.kill_switch import KillSwitch, KillSwitchTrigger

def test_clob_trader_auth_error(monkeypatch):
    """ClobTrader raises AuthError when no private key is set."""
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    # Ensure config file doesn't have it either
    with pytest.raises(AuthError, match="POLYMARKET_PRIVATE_KEY"):
        ClobTrader(credentials_path="non_existent.yaml")

def test_pusd_contract_address():
    """pUSD contract is the V2 collateral address."""
    assert PUSD_CONTRACT == "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

def test_slippage_guard_alignment():
    guard = SlippageGuard(tick_size=0.01)
    assert guard.align_price(0.355) == 0.36
    assert guard.align_price(0.35) == 0.35

def test_slippage_guard_validation():
    guard = SlippageGuard(max_slippage=0.02)
    check = guard.validate(price=0.34, midpoint=0.33)
    assert check.passed
    check_fail = guard.validate(price=0.40, midpoint=0.33)
    assert not check_fail.passed

def test_bankroll_drawdown():
    b = Bankroll(initial=100.0)
    b.update_from_chain(balance=90.0)
    assert b.drawdown_pct == 10.0

def test_exposure_manager_caps():
    em = ExposureManager(bankroll=100.0, max_single_market_fraction=0.05)
    # $5 cap per market
    allowed, max_bet, reason = em.check_caps("event1", 10.0)
    assert allowed
    assert max_bet == 5.0
    
    em.add_position("t1", "event1", "b1", "YES", 10, 5.0)
    allowed2, max_bet2, reason2 = em.check_caps("event1", 1.0)
    assert not allowed2

def test_kill_switch_drawdown():
    ks = KillSwitch(max_drawdown_24h_pct=8.0)
    assert not ks.is_tripped
    ks.check_drawdown(10.0)
    assert ks.is_tripped
    assert ks.trigger == KillSwitchTrigger.DRAWDOWN
