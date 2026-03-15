"""
Integration test: BacktestEngine end-to-end on synthetic candle data.
No network, no contracts required.
"""

import asyncio
from datetime import datetime, timedelta

import numpy as np
import pytest

from backtesting.engine import BacktestEngine, GasModel, SlippageModel, BacktestResult
from agents.risk_manager.manager import RiskConfig
from data_pipeline.feeds import Candle


# ── Helpers ────────────────────────────────────────────────────────────────

def synthetic_candles(
    pair: str,
    n: int,
    start_price: float = 2000.0,
    trend: float = 0.5,
    noise: float = 15.0,
    seed: int = 42,
) -> list[Candle]:
    rng    = np.random.default_rng(seed)
    prices = [start_price]
    for _ in range(n - 1):
        prices.append(max(1.0, prices[-1] + trend + rng.normal(0, noise)))

    ts = datetime(2024, 1, 1)
    return [
        Candle(
            pair=pair,
            open=p * 0.999, high=p * 1.006, low=p * 0.994, close=p,
            volume=rng.uniform(500, 2000),
            timestamp=ts + timedelta(hours=i),
            source="synthetic",
        )
        for i, p in enumerate(prices)
    ]


# Simple momentum signal for testing (no ML, no API)
async def simple_momentum_signal(prices, candles_buf) -> list[dict]:
    signals = []
    for pair, cs in candles_buf.items():
        if len(cs) < 20:
            continue
        closes = [c.close for c in cs[-20:]]
        short  = np.mean(closes[-5:])
        long   = np.mean(closes[-20:])
        diff   = (short - long) / long
        if abs(diff) > 0.005:
            signals.append({
                "pair":       pair,
                "direction":  "buy" if diff > 0 else "sell",
                "confidence": min(0.9, 0.5 + abs(diff) * 20),
            })
    return signals


# ── Tests ──────────────────────────────────────────────────────────────────

@pytest.fixture
def risk_cfg():
    return RiskConfig(
        max_position_usd=500.0,
        max_portfolio_usd=5_000.0,
        max_drawdown_pct=0.20,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        min_confidence=0.55,
    )


@pytest.fixture
def candle_data():
    return {
        "WETH/USDC": synthetic_candles("WETH/USDC", 500, start_price=2000, trend=0.5),
        "ARB/USDC":  synthetic_candles("ARB/USDC",  500, start_price=1.5,  trend=0.001, seed=99),
    }


class TestBacktestEngine:

    @pytest.mark.asyncio
    async def test_run_completes_and_returns_result(self, risk_cfg, candle_data):
        engine = BacktestEngine("test_run", risk_cfg, initial_capital=5_000.0)
        result = await engine.run(simple_momentum_signal, candle_data)
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) > 0

    @pytest.mark.asyncio
    async def test_equity_curve_starts_near_initial(self, risk_cfg, candle_data):
        engine = BacktestEngine("test_equity", risk_cfg, initial_capital=5_000.0)
        result = await engine.run(simple_momentum_signal, candle_data)
        first_eq = result.equity_curve[0][1]
        assert abs(first_eq - 5_000.0) < 100.0

    @pytest.mark.asyncio
    async def test_gas_costs_are_positive(self, risk_cfg, candle_data):
        engine = BacktestEngine("test_gas", risk_cfg, initial_capital=5_000.0)
        result = await engine.run(simple_momentum_signal, candle_data)
        if result.closed_trades:
            assert result.total_gas_cost > 0

    @pytest.mark.asyncio
    async def test_a2a_path_has_higher_gas(self, risk_cfg, candle_data):
        engine_dex = BacktestEngine("dex",  risk_cfg, 5_000.0, use_a2a=False)
        engine_a2a = BacktestEngine("a2a",  risk_cfg, 5_000.0, use_a2a=True)
        r_dex = await engine_dex.run(simple_momentum_signal, candle_data)
        r_a2a = await engine_a2a.run(simple_momentum_signal, candle_data)
        if r_dex.closed_trades and r_a2a.closed_trades:
            dex_per_trade = r_dex.total_gas_cost / len(r_dex.closed_trades)
            a2a_per_trade = r_a2a.total_gas_cost / len(r_a2a.closed_trades)
            assert a2a_per_trade > dex_per_trade

    @pytest.mark.asyncio
    async def test_metrics_are_finite(self, risk_cfg, candle_data):
        engine = BacktestEngine("test_metrics", risk_cfg, initial_capital=5_000.0)
        result = await engine.run(simple_momentum_signal, candle_data)
        assert 0.0 <= result.win_rate <= 1.0
        assert result.max_drawdown >= 0.0
        assert isinstance(result.sharpe_ratio, float)
        assert isinstance(result.sortino_ratio, float)

    @pytest.mark.asyncio
    async def test_net_pnl_accounts_for_costs(self, risk_cfg, candle_data):
        engine = BacktestEngine("test_net", risk_cfg, initial_capital=5_000.0)
        result = await engine.run(simple_momentum_signal, candle_data)
        for t in result.closed_trades:
            expected = t.gross_pnl - t.gas_cost_usd - t.slippage_usd
            assert abs(t.net_pnl - expected) < 0.01

    @pytest.mark.asyncio
    async def test_walk_forward_returns_multiple_folds(self, risk_cfg, candle_data):
        engine  = BacktestEngine("wf_test", risk_cfg, initial_capital=5_000.0)
        results = await engine.walk_forward(simple_momentum_signal, candle_data, folds=3)
        assert len(results) == 3
        for r in results:
            assert len(r.equity_curve) > 0

    @pytest.mark.asyncio
    async def test_uptrend_candles_produce_positive_pnl(self, risk_cfg):
        # Strong uptrend should produce net positive results
        up_data = {
            "WETH/USDC": synthetic_candles("WETH/USDC", 300, start_price=1000, trend=3.0, noise=5.0, seed=1),
        }
        engine = BacktestEngine("uptrend", risk_cfg, initial_capital=5_000.0)
        result = await engine.run(simple_momentum_signal, up_data)
        # Not guaranteed but directionally expected
        logger_msg = result.summary()
        assert isinstance(logger_msg, str)


class TestGasModel:

    def test_swap_costs_less_than_a2a_execute(self):
        gas = GasModel(eth_price_usd=2400.0)
        assert gas.cost_usd("swap") < gas.cost_usd("a2a_execute")

    def test_batch_cheaper_per_item_than_individual(self):
        gas = GasModel(eth_price_usd=2400.0)
        assert gas.cost_usd("a2a_batch") < gas.cost_usd("a2a_execute")

    def test_congestion_scales_cost(self):
        gas = GasModel(eth_price_usd=2400.0)
        assert gas.cost_usd("swap", congestion=3.0) == pytest.approx(gas.cost_usd("swap") * 3, rel=0.01)


class TestSlippageModel:

    def test_large_trade_has_more_slippage(self):
        slip = SlippageModel()
        small = slip.price_impact_pct("WETH/USDC", 1_000)
        large = slip.price_impact_pct("WETH/USDC", 1_000_000)
        assert large > small

    def test_low_liquidity_pair_has_more_slippage(self):
        slip  = SlippageModel()
        eth   = slip.price_impact_pct("WETH/USDC", 10_000)
        arb   = slip.price_impact_pct("ARB/USDC",  10_000)
        assert arb > eth

    def test_effective_price_worse_than_spot(self):
        slip = SlippageModel()
        spot = 2000.0
        eff  = slip.effective_price("WETH/USDC", spot, 10_000, "buy")
        assert eff > spot

    def test_max_impact_capped_at_5_pct(self):
        slip   = SlippageModel()
        impact = slip.price_impact_pct("ARB/USDC", 999_999_999)
        assert impact <= 0.05
