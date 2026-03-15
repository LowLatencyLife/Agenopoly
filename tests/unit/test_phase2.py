"""
Unit tests for Phase 2 components: Indicators, SignalEngine, RiskManager.
No network required — all data is synthetic.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from agents.market_analyst.signals import Indicators, SignalEngine, AggregatedSignal
from agents.risk_manager.manager import RiskManager, RiskConfig, Position
from data_pipeline.feeds import Candle


# ── Fixtures ───────────────────────────────────────────────────────────────

def make_candles(n: int, start_price: float = 2000.0, trend: float = 0.0) -> list[Candle]:
    prices = [start_price + trend * i + np.random.randn() * 10 for i in range(n)]
    ts     = datetime(2024, 1, 1)
    return [
        Candle(
            pair="WETH/USDC",
            open=p * 0.999, high=p * 1.005, low=p * 0.995, close=p,
            volume=1000 + np.random.rand() * 500,
            timestamp=ts + timedelta(hours=i),
            source="test",
        )
        for i, p in enumerate(prices)
    ]


@pytest.fixture
def risk_config():
    return RiskConfig(
        max_position_usd=1000.0,
        max_portfolio_usd=10_000.0,
        max_drawdown_pct=0.15,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        min_confidence=0.50,
    )


@pytest.fixture
def rm(risk_config):
    return RiskManager(risk_config, initial_capital=10_000.0)


# ── Indicators ─────────────────────────────────────────────────────────────

class TestIndicators:

    def test_rsi_neutral_random(self):
        closes = np.random.randn(50).cumsum() + 100
        rsi = Indicators.rsi(closes)
        assert 0 <= rsi <= 100

    def test_rsi_oversold_on_downtrend(self):
        closes = np.linspace(200, 50, 60)
        rsi = Indicators.rsi(closes)
        assert rsi < 35, f"Expected RSI < 35 on downtrend, got {rsi:.1f}"

    def test_rsi_overbought_on_uptrend(self):
        closes = np.linspace(50, 300, 60)
        rsi = Indicators.rsi(closes)
        assert rsi > 65, f"Expected RSI > 65 on uptrend, got {rsi:.1f}"

    def test_macd_returns_three_values(self):
        closes = np.random.randn(60).cumsum() + 100
        m, s, h = Indicators.macd(closes)
        assert isinstance(m, float)
        assert isinstance(s, float)
        assert isinstance(h, float)
        assert abs(h - (m - s)) < 1e-9

    def test_bollinger_ordering(self):
        closes = np.random.randn(50).cumsum() + 100
        upper, mid, lower = Indicators.bollinger(closes)
        assert upper > mid > lower

    def test_atr_positive(self):
        candles = make_candles(30)
        highs  = np.array([c.high  for c in candles])
        lows   = np.array([c.low   for c in candles])
        closes = np.array([c.close for c in candles])
        atr = Indicators.atr(highs, lows, closes)
        assert atr > 0

    def test_obv_rises_on_uptrend_with_volume(self):
        closes  = np.linspace(100, 200, 50)
        volumes = np.ones(50) * 1000
        z = Indicators.obv(closes, volumes)
        assert z > 0

    def test_volume_spike_detects_anomaly(self):
        volumes = np.ones(30) * 1000
        volumes[-1] = 10_000   # 10x spike
        spike = Indicators.volume_spike(volumes)
        assert spike > 2.0


# ── Signal engine ──────────────────────────────────────────────────────────

class TestSignalEngine:

    def _make_engine(self, candles: list[Candle]) -> SignalEngine:
        pipeline = MagicMock()
        pipeline.history = AsyncMock(return_value=candles)
        return SignalEngine(pipeline, anthropic_api_key="")

    @pytest.mark.asyncio
    async def test_returns_aggregated_signal(self):
        candles = make_candles(100)
        engine  = self._make_engine(candles)
        signal  = await engine.compute("WETH/USDC")
        assert isinstance(signal, AggregatedSignal)
        assert signal.direction in ("buy", "sell", "neutral")
        assert 0.0 <= signal.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_strong_uptrend_favors_buy(self):
        candles = make_candles(100, start_price=1000.0, trend=10.0)
        engine  = self._make_engine(candles)
        signal  = await engine.compute("WETH/USDC")
        # Strong uptrend should lean buy — at least not confidently sell
        if signal.direction == "sell":
            assert signal.confidence < 0.5

    @pytest.mark.asyncio
    async def test_low_candle_count_returns_neutral(self):
        candles = make_candles(10)   # too few
        engine  = self._make_engine(candles)
        signal  = await engine.compute("WETH/USDC")
        assert signal.direction == "neutral"
        assert signal.confidence == 0.0

    @pytest.mark.asyncio
    async def test_components_sum_to_reasonable_confidence(self):
        candles = make_candles(150)
        engine  = self._make_engine(candles)
        signal  = await engine.compute("WETH/USDC")
        assert len(signal.components) >= 6
        for c in signal.components:
            assert 0.0 <= c.confidence <= 1.0


# ── Risk manager ───────────────────────────────────────────────────────────

class TestRiskManager:

    def test_approve_valid_signal(self, rm):
        ok, reason = rm.approve("WETH/USDC", "buy", 0.75, 500.0)
        assert ok, reason

    def test_reject_low_confidence(self, rm):
        ok, reason = rm.approve("WETH/USDC", "buy", 0.30, 500.0)
        assert not ok
        assert "confidence" in reason.lower()

    def test_reject_oversized_position(self, rm):
        ok, reason = rm.approve("WETH/USDC", "buy", 0.80, 5_000.0)
        assert not ok
        assert "max" in reason.lower()

    def test_position_size_scales_with_confidence(self, rm):
        size_low  = rm.position_size(0.55, atr=50.0, price=2000.0)
        size_high = rm.position_size(0.90, atr=50.0, price=2000.0)
        assert size_high > size_low

    def test_position_size_scales_down_with_high_atr(self, rm):
        size_calm    = rm.position_size(0.75, atr=10.0,  price=2000.0)
        size_volatile = rm.position_size(0.75, atr=200.0, price=2000.0)
        assert size_calm > size_volatile

    def test_open_and_close_updates_capital(self, rm):
        initial = rm.capital
        rm.open_position("WETH/USDC", "buy", 1000.0, 2000.0)
        rm.update_prices({"WETH/USDC": 2100.0})
        pnl = rm.close_position("WETH/USDC", "test")
        assert pnl is not None
        assert abs(rm.capital - (initial + pnl)) < 0.01

    def test_stop_loss_triggers(self, rm):
        rm.open_position("WETH/USDC", "buy", 1000.0, 2000.0)
        # Price drops 6% — below 5% stop
        rm.update_prices({"WETH/USDC": 1880.0})
        exits = rm.check_exits()
        assert any(pair == "WETH/USDC" for pair, _ in exits)
        assert any(reason == "stop_loss" for _, reason in exits)

    def test_take_profit_triggers(self, rm):
        rm.open_position("WETH/USDC", "buy", 1000.0, 2000.0)
        rm.update_prices({"WETH/USDC": 2300.0})   # +15% > 10% TP
        exits = rm.check_exits()
        assert any(reason == "take_profit" for _, reason in exits)

    def test_drawdown_halts_trading(self, rm):
        # Simulate losses until drawdown limit
        rm.capital = 8_000.0   # 20% loss from 10k
        rm.peak_equity = 10_000.0
        ok, reason = rm.approve("WETH/USDC", "buy", 0.80, 500.0)
        assert not ok
        assert "drawdown" in reason.lower() or rm.is_halted

    def test_max_positions_limit(self, rm):
        pairs = ["WETH/USDC", "WBTC/USDC", "ARB/USDC", "LINK/USDC", "UNI/USDC"]
        for p in pairs:
            rm.open_position(p, "buy", 200.0, 100.0)
        ok, reason = rm.approve("AAVE/USDC", "buy", 0.80, 200.0)
        assert not ok
        assert "max open" in reason.lower()

    def test_snapshot_reflects_unrealized_pnl(self, rm):
        rm.open_position("WETH/USDC", "buy", 1000.0, 2000.0)
        rm.update_prices({"WETH/USDC": 2200.0})
        snap = rm.snapshot()
        assert snap.unrealized_pnl > 0
        assert snap.open_positions == 1
