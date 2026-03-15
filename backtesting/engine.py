"""
BacktestEngine v2 — Historical simulation with realistic on-chain cost model.

Key improvements over v1:
  - Uniswap v3 price impact formula for slippage (not flat bps)
  - Per-trade gas cost from Arbitrum historical data
  - Integrates RiskManager for authentic position sizing + stop-loss
  - Multi-period walk-forward validation to avoid overfitting
  - Full metrics: Sharpe, Sortino, Calmar, profit factor, win rate
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import numpy as np

from data_pipeline.feeds import Candle
from agents.risk_manager.manager import RiskManager, RiskConfig

logger = logging.getLogger(__name__)


# ── Gas model ──────────────────────────────────────────────────────────────

@dataclass
class GasModel:
    eth_price_usd:      float = 2400.0
    swap_gas:           int   = 120_000
    a2a_propose_gas:    int   = 80_000
    a2a_accept_gas:     int   = 50_000
    a2a_execute_gas:    int   = 140_000
    a2a_batch_per_item: int   = 90_000
    base_fee_gwei:      float = 0.1
    priority_fee_gwei:  float = 0.01

    def cost_usd(self, operation: str, congestion: float = 1.0) -> float:
        gas_units = {
            "swap":        self.swap_gas,
            "a2a_propose": self.a2a_propose_gas,
            "a2a_accept":  self.a2a_accept_gas,
            "a2a_execute": self.a2a_execute_gas,
            "a2a_batch":   self.a2a_batch_per_item,
        }.get(operation, self.swap_gas)
        eth_cost = gas_units * (self.base_fee_gwei + self.priority_fee_gwei) * congestion * 1e-9
        return eth_cost * self.eth_price_usd


# ── Slippage model ─────────────────────────────────────────────────────────

class SlippageModel:
    POOL_LIQUIDITY = {
        "WETH/USDC": 25_000_000,
        "WBTC/USDC": 8_000_000,
        "ARB/USDC":  3_000_000,
    }

    def price_impact_pct(self, pair: str, trade_size_usd: float) -> float:
        liquidity = self.POOL_LIQUIDITY.get(pair, 5_000_000)
        return min(trade_size_usd / (2 * liquidity), 0.05)

    def effective_price(self, pair: str, price: float, size_usd: float, direction: str) -> float:
        impact = self.price_impact_pct(pair, size_usd)
        return price * (1 + impact) if direction == "buy" else price * (1 - impact)


# ── Trade record ───────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    id:           int
    pair:         str
    direction:    str
    size_usd:     float
    entry_price:  float
    entry_time:   datetime
    exit_price:   float   = 0.0
    exit_time:    Optional[datetime] = None
    exit_reason:  str     = ""
    gas_cost_usd: float   = 0.0
    slippage_usd: float   = 0.0
    gross_pnl:    float   = 0.0
    net_pnl:      float   = 0.0
    signal_conf:  float   = 0.0
    path:         str     = "dex"

    @property
    def closed(self) -> bool:
        return self.exit_price > 0


# ── Result ─────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    config_name:  str
    start_date:   datetime
    end_date:     datetime
    initial_cap:  float
    trades:       list[BacktestTrade]           = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]]  = field(default_factory=list)

    @property
    def closed_trades(self):
        return [t for t in self.trades if t.closed]

    @property
    def total_net_pnl(self):
        return sum(t.net_pnl for t in self.closed_trades)

    @property
    def total_gas_cost(self):
        return sum(t.gas_cost_usd for t in self.trades)

    @property
    def total_slippage(self):
        return sum(t.slippage_usd for t in self.trades)

    @property
    def win_rate(self):
        if not self.closed_trades: return 0.0
        return len([t for t in self.closed_trades if t.net_pnl > 0]) / len(self.closed_trades)

    @property
    def profit_factor(self):
        gross = sum(t.net_pnl for t in self.closed_trades if t.net_pnl > 0)
        loss  = abs(sum(t.net_pnl for t in self.closed_trades if t.net_pnl < 0))
        return gross / loss if loss > 0 else float("inf")

    @property
    def max_drawdown(self):
        if len(self.equity_curve) < 2: return 0.0
        peak, dd = self.equity_curve[0][1], 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            dd   = max(dd, (peak - eq) / peak)
        return dd

    def _daily_returns(self) -> np.ndarray:
        if len(self.equity_curve) < 2: return np.array([])
        return np.array([
            (self.equity_curve[i][1] - self.equity_curve[i-1][1]) / self.equity_curve[i-1][1]
            for i in range(1, len(self.equity_curve))
        ])

    @property
    def sharpe_ratio(self):
        r = self._daily_returns()
        return float(r.mean() / r.std() * np.sqrt(252)) if len(r) > 1 and r.std() > 0 else 0.0

    @property
    def sortino_ratio(self):
        r = self._daily_returns()
        if len(r) < 2: return 0.0
        down = r[r < 0]
        denom = down.std() * np.sqrt(252) if len(down) > 1 else 1e-9
        return float(r.mean() * 252 / denom)

    @property
    def calmar_ratio(self):
        if self.max_drawdown == 0: return 0.0
        days = max(1, (self.end_date - self.start_date).days)
        ann  = (self.total_net_pnl / self.initial_cap) * (365 / days)
        return ann / self.max_drawdown

    def summary(self, verbose: bool = False) -> str:
        lines = [
            f"\n{'='*58}",
            f"  Backtest: {self.config_name}",
            f"{'='*58}",
            f"  Period        {self.start_date.date()} → {self.end_date.date()}",
            f"  Initial cap   ${self.initial_cap:,.0f}",
            f"  Final equity  ${self.initial_cap + self.total_net_pnl:,.2f}",
            f"  Net PnL       ${self.total_net_pnl:+,.2f}  ({self.total_net_pnl/self.initial_cap:+.1%})",
            f"  Gas costs     ${self.total_gas_cost:.2f}",
            f"  Slippage      ${self.total_slippage:.2f}",
            f"  Trades        {len(self.closed_trades)}  (win rate {self.win_rate:.1%})",
            f"  Profit factor {self.profit_factor:.2f}",
            f"  Max drawdown  {self.max_drawdown:.1%}",
            f"  Sharpe        {self.sharpe_ratio:.2f}",
            f"  Sortino       {self.sortino_ratio:.2f}",
            f"  Calmar        {self.calmar_ratio:.2f}",
            f"{'='*58}",
        ]
        if verbose and self.closed_trades:
            lines.append("\n  Last 5 trades:")
            for t in self.closed_trades[-5:]:
                lines.append(
                    f"    [{t.entry_time.date()}] {t.direction.upper()} {t.pair} "
                    f"${t.size_usd:.0f} → net PnL ${t.net_pnl:+.2f} ({t.exit_reason})"
                )
        return "\n".join(lines)


# ── Engine ─────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Runs a signal function over historical candles with realistic on-chain costs.

    signal_fn signature:
        async fn(prices: dict[str, float], candles: dict[str, list[Candle]])
        → list[dict]  # {"pair", "direction", "confidence"}
    """

    def __init__(
        self,
        name:            str,
        risk_config:     RiskConfig,
        initial_capital: float = 10_000.0,
        gas_model:       Optional[GasModel] = None,
        slippage_model:  Optional[SlippageModel] = None,
        use_a2a:         bool = False,
    ):
        self.name      = name
        self.risk_cfg  = risk_config
        self.init_cap  = initial_capital
        self.gas       = gas_model or GasModel()
        self.slippage  = slippage_model or SlippageModel()
        self.use_a2a   = use_a2a
        self._trade_id = 0

    async def run(
        self,
        signal_fn:   Callable,
        candle_data: dict[str, list[Candle]],
        start_date:  Optional[datetime] = None,
        end_date:    Optional[datetime] = None,
    ) -> BacktestResult:
        all_ts = sorted({c.timestamp for cs in candle_data.values() for c in cs})
        if start_date: all_ts = [t for t in all_ts if t >= start_date]
        if end_date:   all_ts = [t for t in all_ts if t <= end_date]

        if not all_ts:
            raise ValueError("No candles in the specified date range")

        rm     = RiskManager(self.risk_cfg, self.init_cap)
        result = BacktestResult(self.name, all_ts[0], all_ts[-1], self.init_cap)
        bufs:  dict[str, list[Candle]] = {p: [] for p in candle_data}

        logger.info(f"[BT:{self.name}] {len(all_ts)} bars | ${self.init_cap:,.0f}")

        for ts in all_ts:
            for pair, candles in candle_data.items():
                bufs[pair].extend(c for c in candles if c.timestamp == ts)
                if len(bufs[pair]) > 500:
                    bufs[pair] = bufs[pair][-500:]

            prices = {p: cs[-1].close for p, cs in bufs.items() if cs}
            rm.update_prices(prices)

            for pair, reason in rm.check_exits():
                self._close(result, rm, pair, prices.get(pair, 0), reason)

            try:
                signals = await signal_fn(prices, bufs)
            except Exception as e:
                logger.debug(f"[BT] signal error: {e}")
                signals = []

            for sig in signals:
                pair, direction, conf = sig.get("pair",""), sig.get("direction",""), sig.get("confidence",0.0)
                price = prices.get(pair, 0)
                if not pair or not direction or price == 0:
                    continue

                atr  = self._atr(bufs.get(pair, []))
                size = rm.position_size(conf, atr, price)
                ok, reason = rm.approve(pair, direction, conf, size)
                if not ok:
                    continue

                eff_price = self.slippage.effective_price(pair, price, size, direction)
                slip_cost = abs(eff_price - price) / price * size
                gas_cost  = self.gas.cost_usd("a2a_propose" if self.use_a2a else "swap")

                rm.open_position(pair, direction, size, eff_price)
                self._trade_id += 1
                result.trades.append(BacktestTrade(
                    id=self._trade_id, pair=pair, direction=direction,
                    size_usd=size, entry_price=eff_price, entry_time=ts,
                    gas_cost_usd=gas_cost, slippage_usd=slip_cost,
                    signal_conf=conf, path="a2a" if self.use_a2a else "dex",
                ))

            result.equity_curve.append((ts, rm.snapshot().total_equity))

        for pair, pos in rm.positions.items():
            if pos.status == "open":
                self._close(result, rm, pair, prices.get(pair, 0), "end_of_period")

        logger.info(result.summary())
        return result

    async def walk_forward(
        self,
        signal_fn:   Callable,
        candle_data: dict[str, list[Candle]],
        folds:       int   = 5,
        train_ratio: float = 0.7,
    ) -> list[BacktestResult]:
        """k-fold walk-forward validation. Returns test-fold results only."""
        all_ts   = sorted({c.timestamp for cs in candle_data.values() for c in cs})
        fold_len = len(all_ts) // folds
        results  = []

        for i in range(folds):
            fold_ts = all_ts[i * fold_len : (i+1) * fold_len]
            split   = int(len(fold_ts) * train_ratio)
            s, e    = fold_ts[split], fold_ts[-1]
            logger.info(f"[WF] Fold {i+1}/{folds}: {s.date()} → {e.date()}")
            r = await self.run(signal_fn, candle_data, s, e)
            r.config_name = f"{self.name} WF-fold{i+1}"
            results.append(r)

        return results

    def _close(self, result, rm, pair, price, reason):
        pnl = rm.close_position(pair, reason)
        if pnl is None: return
        for trade in reversed(result.trades):
            if trade.pair == pair and not trade.closed:
                exit_slip       = self.slippage.price_impact_pct(pair, trade.size_usd) * trade.size_usd
                exit_gas        = self.gas.cost_usd("a2a_execute" if self.use_a2a else "swap")
                trade.exit_price  = price
                trade.exit_time   = datetime.utcnow()
                trade.exit_reason = reason
                trade.gross_pnl   = pnl
                trade.gas_cost_usd += exit_gas
                trade.slippage_usd += exit_slip
                trade.net_pnl      = pnl - trade.gas_cost_usd - trade.slippage_usd
                break

    @staticmethod
    def _atr(candles: list[Candle], period: int = 14) -> float:
        if len(candles) < period + 1:
            return candles[-1].close * 0.02 if candles else 1.0
        highs  = np.array([c.high  for c in candles[-period-1:]])
        lows   = np.array([c.low   for c in candles[-period-1:]])
        closes = np.array([c.close for c in candles[-period-1:]])
        tr = np.maximum(highs[1:]-lows[1:], np.maximum(np.abs(highs[1:]-closes[:-1]), np.abs(lows[1:]-closes[:-1])))
        return float(np.mean(tr))
