"""
Unit tests for WeatherStrategyEngine.

Tests the three core stages against worked examples from
PHASE1_FOUNDATIONS.md, plus edge cases.

Run: pytest tests/test_strategy_engine.py -v
"""

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

from src.strategy.engine import WeatherStrategyEngine, StrategyConfig


# --- Fixtures ---

@pytest.fixture
def default_config():
    return StrategyConfig(
        min_ensemble_size=30,
        calibration_alpha=0.85,
        prob_clip_min=0.01,
        prob_clip_max=0.99,
        min_ev_per_share=0.05,
        kelly_fraction=0.25,
        max_single_market_fraction=0.05,
        max_total_exposure_fraction=0.25,
        min_bet_dollars=1.00,
        liquidity_depth_levels=3,
        bankroll=1000.0,
    )


@pytest.fixture
def engine(default_config):
    return WeatherStrategyEngine(default_config)


@pytest.fixture
def config_from_yaml():
    return {
        "strategy": {
            "min_ev_per_share": 0.05,
            "prob_clip_min": 0.01,
            "prob_clip_max": 0.99,
            "calibration_alpha": 0.85,
        },
        "risk": {
            "bankroll": 1000,
            "kelly_fraction": 0.25,
            "max_single_market_fraction": 0.05,
            "max_total_exposure_fraction": 0.25,
        },
    }


# --- Stage 1: Probability Divergence ---

class TestProbabilityDivergence:

    def test_temp_in_bucket_closed_range(self):
        assert WeatherStrategyEngine._temp_in_bucket(86.0, 86, 87)
        assert WeatherStrategyEngine._temp_in_bucket(87.0, 86, 87)
        assert WeatherStrategyEngine._temp_in_bucket(86.5, 86, 87)
        assert not WeatherStrategyEngine._temp_in_bucket(85.9, 86, 87)
        assert not WeatherStrategyEngine._temp_in_bucket(87.1, 86, 87)

    def test_temp_in_bucket_open_low(self):
        assert WeatherStrategyEngine._temp_in_bucket(75.0, -math.inf, 77)
        assert WeatherStrategyEngine._temp_in_bucket(77.0, -math.inf, 77)
        assert not WeatherStrategyEngine._temp_in_bucket(78.0, -math.inf, 77)

    def test_temp_in_bucket_open_high(self):
        assert WeatherStrategyEngine._temp_in_bucket(96.0, 96, math.inf)
        assert WeatherStrategyEngine._temp_in_bucket(100.0, 96, math.inf)
        assert not WeatherStrategyEngine._temp_in_bucket(95.9, 96, math.inf)

    def test_ensemble_probability_all_in_bucket(self, engine):
        """All models in one bucket -> high p_model."""
        df = pd.DataFrame([
            {"bucket_label": "86-87", "bucket_low": 86, "bucket_high": 87,
             "model_name": "gfs", "model_temp_mkt": 86.5},
            {"bucket_label": "86-87", "bucket_low": 86, "bucket_high": 87,
             "model_name": "ecmwf", "model_temp_mkt": 87.0},
            {"bucket_label": "86-87", "bucket_low": 86, "bucket_high": 87,
             "model_name": "icon", "model_temp_mkt": 86.0},
            {"bucket_label": "84-85", "bucket_low": 84, "bucket_high": 85,
             "model_name": "gfs", "model_temp_mkt": 84.5},
            {"bucket_label": "84-85", "bucket_low": 84, "bucket_high": 85,
             "model_name": "ecmwf", "model_temp_mkt": 85.0},
            {"bucket_label": "84-85", "bucket_low": 84, "bucket_high": 85,
             "model_name": "icon", "model_temp_mkt": 84.0},
        ])
        p, n, temps = engine.compute_ensemble_probability(
            df, "86-87", 86, 87, "F"
        )
        assert n == 3
        assert p > 0.5

    def test_ensemble_probability_none_in_bucket(self, engine):
        """No models in this bucket -> low p_model (with 11 buckets)."""
        all_labels = ["<=77","78-79","80-81","82-83","84-85","86-87",
                       "88-89","90-91","92-93","94-95",">=96"]
        all_bounds = [(-math.inf,77),(78,79),(80,81),(82,83),(84,85),
                       (86,87),(88,89),(90,91),(92,93),(94,95),(96,math.inf)]
        rows = []
        for label, (lo, hi) in zip(all_labels, all_bounds):
            for mn, mt in [("gfs",70.0),("ecmwf",72.0)]:
                rows.append({
                    "bucket_label": label, "bucket_low": lo,
                    "bucket_high": hi, "model_name": mn,
                    "model_temp_mkt": mt,
                })
        df = pd.DataFrame(rows)
        p, n, temps = engine.compute_ensemble_probability(
            df, "86-87", 86, 87, "F"
        )
        assert n == 2
        assert p < 0.15

    def test_ensemble_probability_empty(self, engine):
        df = pd.DataFrame(columns=["bucket_label", "model_name",
                                   "model_temp_mkt"])
        p, n, temps = engine.compute_ensemble_probability(
            df, "86-87", 86, 87, "F"
        )
        assert p == 0.0
        assert n == 0
        assert temps == []

    def test_divergence_positive(self, engine):
        d = engine.compute_divergence(p_model=0.75, p_market=0.30)
        assert abs(d - 0.45) < 0.001

    def test_divergence_negative(self, engine):
        d = engine.compute_divergence(p_model=0.20, p_market=0.60)
        assert abs(d - (-0.40)) < 0.001

    def test_divergence_zero(self, engine):
        d = engine.compute_divergence(p_model=0.50, p_market=0.50)
        assert d == 0.0


# --- Stage 2: EV Calculator ---

class TestEVCalculator:

    def test_ev_yes_positive(self, engine):
        """Worked example: p_model=0.75, ask=0.30 -> EV=+0.45."""
        ev = engine.compute_ev(p_model=0.75, ask_price=0.30)
        assert abs(ev - 0.45) < 0.001

    def test_ev_no_negative(self, engine):
        """NO side: p_model_no=0.25, ask=0.70 -> EV=-0.45."""
        ev = engine.compute_ev(p_model=0.25, ask_price=0.70)
        assert abs(ev - (-0.45)) < 0.001

    def test_ev_zero_when_equal(self, engine):
        ev = engine.compute_ev(p_model=0.50, ask_price=0.50)
        assert abs(ev) < 0.001

    def test_find_edge_yes_wins(self, engine):
        side, ev, ask = engine.find_edge(
            p_model=0.75, ask_yes=0.30, ask_no=0.70
        )
        assert side == "YES"
        assert ev > 0.05
        assert abs(ask - 0.30) < 0.001

    def test_find_edge_no_wins(self, engine):
        """p_model=0.20 -> p_no=0.80, ask_no=0.30 -> ev_no=0.50."""
        side, ev, ask = engine.find_edge(
            p_model=0.20, ask_yes=0.80, ask_no=0.30
        )
        assert side == "NO"
        assert ev > 0.05

    def test_find_edge_skip_below_threshold(self, engine):
        side, ev, ask = engine.find_edge(
            p_model=0.50, ask_yes=0.50, ask_no=0.50
        )
        assert side == "SKIP"
        assert ev == 0.0

    def test_find_edge_skip_on_small_edge(self, engine):
        """EV = 0.03 < 0.05 threshold -> SKIP."""
        side, ev, ask = engine.find_edge(
            p_model=0.53, ask_yes=0.50, ask_no=0.50
        )
        assert side == "SKIP"

    def test_find_edge_yes_at_threshold(self, engine):
        """EV just below threshold (0.049) -> SKIP."""
        side, ev, ask = engine.find_edge(
            p_model=0.549, ask_yes=0.50, ask_no=0.50
        )
        # ev_yes = 0.049 < 0.05
        assert side == "SKIP"

    def test_find_edge_just_above_threshold(self, engine):
        """EV = 0.06 > 0.05 -> YES."""
        side, ev, ask = engine.find_edge(
            p_model=0.56, ask_yes=0.50, ask_no=0.50
        )
        assert side == "YES"


# --- Stage 3: Fractional Kelly Sizing ---

class TestKellySizing:

    def test_full_kelly_worked_example(self, engine):
        """Full Kelly: p=0.75, price=0.31 -> f*=0.6377."""
        f = engine.full_kelly_fraction(p_model=0.75, price=0.31)
        assert abs(f - 0.6377) < 0.01

    def test_full_kelly_no_edge(self, engine):
        f = engine.full_kelly_fraction(p_model=0.30, price=0.50)
        assert f < 0

    def test_full_kelly_extreme_prices(self, engine):
        assert engine.full_kelly_fraction(0.75, 0.0) == 0.0
        assert engine.full_kelly_fraction(0.75, 1.0) == 0.0

    def test_size_position_market_cap_binds(self, engine):
        """
        Worked example from PHASE1_FOUNDATIONS.md:
        p_model=0.75, ask=0.31, bankroll=$1000
        Full Kelly = 0.6377, Quarter = $159.40
        Market cap = $50, Liquidity = $155
        Final = $50 (market cap binds)
        """
        sizing = engine.size_position(
            p_model=0.75, ask_price=0.31,
            ask_depth=500, current_total_exposure=0.0,
        )
        assert not sizing["skipped"]
        assert abs(sizing["f_star"] - 0.6377) < 0.01
        assert abs(sizing["kelly_dollars"] - 159.4) < 5.0
        assert abs(sizing["single_market_cap"] - 50.0) < 0.01
        assert abs(sizing["liquidity_cap"] - 155.0) < 0.01
        assert abs(sizing["final_bet_dollars"] - 50.0) < 0.01
        assert sizing["binding_cap"] == "market"
        assert abs(sizing["shares"] - 161.29) < 1.0
        assert abs(sizing["max_loss"] - 50.0) < 0.01

    def test_size_position_liquidity_cap_binds(self, engine):
        sizing = engine.size_position(
            p_model=0.75, ask_price=0.31,
            ask_depth=100,  # 100*0.31=$31 < $50 market cap
            current_total_exposure=0.0,
        )
        assert not sizing["skipped"]
        assert sizing["binding_cap"] == "liquidity"
        assert abs(sizing["final_bet_dollars"] - 31.0) < 0.01

    def test_size_position_kelly_cap_binds(self, engine):
        sizing = engine.size_position(
            p_model=0.56, ask_price=0.50,
            ask_depth=10000,
            current_total_exposure=0.0,
        )
        if not sizing["skipped"]:
            assert sizing["binding_cap"] == "kelly"

    def test_size_position_no_edge_skips(self, engine):
        sizing = engine.size_position(
            p_model=0.30, ask_price=0.50,
            ask_depth=1000,
        )
        assert sizing["skipped"]
        assert "No Kelly edge" in sizing["skip_reason"]

    def test_size_position_too_small_skips(self, engine):
        sizing = engine.size_position(
            p_model=0.51, ask_price=0.50,
            ask_depth=1,
            current_total_exposure=0.0,
        )
        assert sizing["skipped"]

    def test_total_exposure_cap(self, engine):
        """$1000 bankroll, 25% max = $250. Start at $230 -> $20 left."""
        sizing = engine.size_position(
            p_model=0.75, ask_price=0.31,
            ask_depth=10000,
            current_total_exposure=230.0,
        )
        assert not sizing["skipped"]
        assert sizing["binding_cap"] == "exposure"
        assert abs(sizing["final_bet_dollars"] - 20.0) < 0.01

    def test_exposure_cap_zero_remaining(self, engine):
        sizing = engine.size_position(
            p_model=0.75, ask_price=0.31,
            ask_depth=10000,
            current_total_exposure=250.0,
        )
        assert sizing["skipped"]


# --- Config ---

class TestConfig:

    def test_from_yaml(self, config_from_yaml):
        cfg = StrategyConfig.from_yaml(config_from_yaml)
        assert cfg.min_ev_per_share == 0.05
        assert cfg.kelly_fraction == 0.25
        assert cfg.max_single_market_fraction == 0.05
        assert cfg.bankroll == 1000.0

    def test_defaults(self):
        cfg = StrategyConfig()
        assert cfg.kelly_fraction == 0.25
        assert cfg.max_single_market_fraction == 0.05
        assert cfg.min_ev_per_share == 0.05
        assert cfg.bankroll == 1000.0

    def test_from_config_classmethod(self, config_from_yaml):
        eng = WeatherStrategyEngine.from_config(config_from_yaml)
        assert eng.config.kelly_fraction == 0.25
        assert eng.config.bankroll == 1000.0


# --- Integration: evaluate() ---

class TestEvaluate:

    def test_evaluate_finds_opportunity(self, engine):
        """
        Synthetic market where 30 models predict 86-87°F (p_model~0.75)
        but market prices it at 0.30. Should find a YES opportunity.
        """
        bucket_labels = ["<=77","78-79","80-81","82-83","84-85",
                         "86-87","88-89","90-91","92-93","94-95",">=96"]
        bucket_bounds = [
            (-math.inf,77),(78,79),(80,81),(82,83),(84,85),
            (86,87),(88,89),(90,91),(92,93),(94,95),(96,math.inf),
        ]
        yes_prices = [0.02,0.03,0.05,0.08,0.10,0.30,0.15,0.12,0.10,0.08,0.05]

        # 30 models, ~22 in 86-87, rest scattered -> p_model ~0.73
        # This avoids calibration shrinkage (ens_size >= min_ensemble_size)
        model_names = [f"model_{i}" for i in range(30)]
        # 22 models at 86.5 (in 86-87), 8 at 70 (in <=77)
        model_temps = [86.5]*22 + [70.0]*8

        rows = []
        for i, label in enumerate(bucket_labels):
            lo, hi = bucket_bounds[i]
            for j, mt in enumerate(model_temps):
                in_bucket = (lo <= mt <= hi) if not math.isinf(lo) and not math.isinf(hi) \
                    else (mt <= hi if math.isinf(lo) else mt >= lo)
                rows.append({
                    "event_slug": "test-nyc",
                    "event_title": "Highest temperature in NYC on April 16?",
                    "station_icao": "KLGA",
                    "target_date": "2026-04-16",
                    "bucket_label": label,
                    "bucket_low": lo,
                    "bucket_high": hi,
                    "units": "F",
                    "token_id": f"token_{i}",
                    "condition_id": f"cond_{i}",
                    "implied_prob": yes_prices[i],
                    "best_bid": yes_prices[i] - 0.01,
                    "best_ask": yes_prices[i] + 0.01,
                    "spread": 0.02,
                    "midpoint": yes_prices[i],
                    "bid_depth": 300.0,
                    "ask_depth": 500.0,
                    "volume": 10000.0,
                    "model_name": model_names[j],
                    "model_temp_c": (mt - 32) * 5 / 9,
                    "model_temp_mkt": mt,
                    "model_in_bucket": in_bucket,
                    "ensemble_size": 30,
                    "fetched_at": "2026-07-07T00:00:00Z",
                })

        df = pd.DataFrame(rows)
        opps = engine.evaluate(df)

        yes_opps = [o for o in opps if o.side == "YES" and not o.skipped]
        assert len(yes_opps) > 0, f"Expected YES opportunities, got {[(o.bucket_label, o.side, o.skipped, o.skip_reason) for o in opps]}"

        target = [o for o in yes_opps if o.bucket_label == "86-87"]
        assert len(target) == 1
        assert target[0].p_model > 0.5
        assert target[0].ev_per_share > 0.05
        assert target[0].final_bet_dollars > 0

    def test_evaluate_empty_df(self, engine):
        opps = engine.evaluate(pd.DataFrame())
        assert opps == []

    def test_evaluate_no_model_data_skips(self, engine):
        """When no model data available, all opportunities should be SKIP."""
        df = pd.DataFrame([{
            "event_slug": "test",
            "station_icao": "KLGA",
            "target_date": "2026-04-16",
            "bucket_label": "86-87",
            "bucket_low": 86,
            "bucket_high": 87,
            "units": "F",
            "token_id": "t",
            "condition_id": "c",
            "implied_prob": 0.30,
            "best_bid": 0.29,
            "best_ask": 0.31,
            "spread": 0.02,
            "midpoint": 0.30,
            "bid_depth": 100.0,
            "ask_depth": 200.0,
            "volume": 5000.0,
            "model_name": None,
            "model_temp_c": None,
            "model_temp_mkt": None,
            "model_in_bucket": None,
            "ensemble_size": 0,
            "fetched_at": "2026-07-07T00:00:00Z",
        }])
        opps = engine.evaluate(df)
        assert len(opps) >= 1
        assert all(o.skipped for o in opps), \
            f"Expected all skipped, got {[o.skip_reason for o in opps]}"

    def test_evaluate_dataframe_returns_df(self, engine):
        df = pd.DataFrame([{
            "event_slug": "test",
            "station_icao": "KLGA",
            "target_date": "2026-04-16",
            "bucket_label": "86-87",
            "bucket_low": 86,
            "bucket_high": 87,
            "units": "F",
            "token_id": "t",
            "condition_id": "c",
            "implied_prob": 0.30,
            "best_bid": 0.29,
            "best_ask": 0.31,
            "spread": 0.02,
            "midpoint": 0.30,
            "bid_depth": 100.0,
            "ask_depth": 200.0,
            "volume": 5000.0,
            "model_name": None,
            "model_temp_c": None,
            "model_temp_mkt": None,
            "model_in_bucket": None,
            "ensemble_size": 0,
            "fetched_at": "2026-07-07T00:00:00Z",
        }])
        result_df = engine.evaluate_dataframe(df)
        assert isinstance(result_df, pd.DataFrame)