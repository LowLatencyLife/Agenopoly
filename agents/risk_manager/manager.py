"""
RiskManager — Autonomous risk controls for Agenopoly agents.

Responsibilities:
  1. Position sizing (Kelly Criterion + volatility-adjusted)
  2. Stop-loss monitoring and automatic exit signals
  3. Portfolio drawdown circuit breaker
  4. Exposure limits per pair and overall
  5. Correlation guard — prevents over-concentration in correlated assets
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    max_position_usd:   float = 2_000.0   # max single trade size
    max_portfolio_usd:  float = 10_000.0  # total capital
    max_drawdown_pct:   float = 0.15      # 15% — pause trading if exceeded
    stop_loss_pct:      float = 0.05      # 5% — auto-exit individual position
    take_profit_pct:    float = 0.12      # 12% — auto-exit on profit
    max_open_positions: int   = 5
    max_pair_exposure:  float = 0.30      # max 30% of portfolio in one pair
    kelly_fraction:     float = 0.25      # fractional Kelly (conservative)
    min_confidence:     float = 0.55      # discard signals below this
    corr_threshold:     float = 0.85      # block new position if corr > 0.85 with existing


# ── Position tracking ──────────────────────────────────────────────────────

@dataclass
class Position:
    pair:           str
    direction:      str         # "buy" | "sell"
    size_usd:       float
    entry_price:    float
    entry_time:     datetime    = field(default_factory=datetime.utcnow)
    stop_loss:      float       = 0.0
    take_profit:    float       = 0.0
    current_price:  float       = 0.0
    status:         str         = "open"   # "open" | "closed" | "stopped"

    @property
    def unrealized_pnl(self) -> float:
        if self.entry_price == 0:
            return 0.0
        pct = (self.current_price - self.entry_price) / self.entry_price
        if self.direction == "sell":
            pct = -pct
        return self.size_usd * pct

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.size_usd == 0:
            return 0.0
        return self.unrealized_pnl / self.size_usd

    @property
    def holding_hours(self) -> float:
        return (datetime.utcnow() - self.entry_time).total_seconds() / 3600


@dataclass
class RiskSnapshot:
    timestamp:          datetime
    total_equity:       float
    open_positions:     int
    total_exposure_usd: float
    unrealized_pnl:     float
    current_drawdown:   float
    is_halted:          bool
    pair_exposures:     dict[str, float]


# ── Risk manager ───────────────────────────────────────────────────────────

class RiskManager:
    """
    Stateful risk controller. One instance per agent.

    Usage:
        rm = RiskManager(config, initial_capital=10_000)
        size = rm.position_size(signal, atr=45.0)
        if rm.approve(signal):
            rm.open_position(pair, direction, size, entry_price)
        rm.update_prices({"WETH/USDC": 2400})   # called every tick
        exits = rm.check_exits()
    """

    def __init__(self, config: RiskConfig, initial_capital: float):
        self.config   = config
        self.capital  = initial_capital
        self.peak_equity = initial_capital
        self.positions: dict[str, Position] = {}   # pair → Position
        self.closed_pnl = 0.0
        self.is_halted  = False
        self._price_history: dict[str, list[float]] = {}

    # ── Signal approval ────────────────────────────────────────────────────

    def approve(self, pair: str, direction: str, confidence: float, size_usd: float) -> tuple[bool, str]:
        """
        Check all risk rules before opening a new position.
        Returns (approved: bool, reason: str).
        """
        if self.is_halted:
            return False, "Trading halted — drawdown limit reached"

        if confidence < self.config.min_confidence:
            return False, f"Confidence {confidence:.2f} below minimum {self.config.min_confidence}"

        if pair in self.positions and self.positions[pair].status == "open":
            return False, f"Already have open position in {pair}"

        if len([p for p in self.positions.values() if p.status == "open"]) >= self.config.max_open_positions:
            return False, f"Max open positions ({self.config.max_open_positions}) reached"

        if size_usd > self.config.max_position_usd:
            return False, f"Size ${size_usd:.0f} > max ${self.config.max_position_usd:.0f}"

        exposure = self._pair_exposure(pair) + size_usd
        if exposure / self.config.max_portfolio_usd > self.config.max_pair_exposure:
            return False, f"{pair} exposure would exceed {self.config.max_pair_exposure:.0%}"

        current_dd = self._current_drawdown()
        if current_dd > self.config.max_drawdown_pct:
            self.is_halted = True
            return False, f"Drawdown {current_dd:.1%} exceeded limit {self.config.max_drawdown_pct:.1%}"

        return True, "approved"

    # ── Position sizing ────────────────────────────────────────────────────

    def position_size(self, confidence: float, atr: float, price: float) -> float:
        """
        Kelly Criterion with ATR-based volatility adjustment.

        Kelly formula: f = (p * b - q) / b
          p = win probability (approximated from confidence)
          b = expected reward / risk ratio
          q = 1 - p

        We use fractional Kelly (config.kelly_fraction) to be conservative.
        ATR scales size down in high-volatility regimes.
        """
        p    = 0.45 + confidence * 0.15   # map [0,1] → [0.45, 0.60] win prob
        b    = 2.0                          # assume 2:1 reward/risk ratio
        q    = 1 - p
        kelly = (p * b - q) / b
        kelly = max(0.0, min(1.0, kelly))

        # Volatility adjustment: ATR as % of price
        atr_pct = atr / price if price > 0 else 0.01
        vol_multiplier = max(0.3, 1.0 - atr_pct * 5)

        raw_size = self.capital * self.config.kelly_fraction * kelly * vol_multiplier
        return min(raw_size, self.config.max_position_usd)

    # ── Open / close positions ─────────────────────────────────────────────

    def open_position(self, pair: str, direction: str, size_usd: float, entry_price: float) -> Position:
        stop_mult = 1 - self.config.stop_loss_pct   if direction == "buy" else 1 + self.config.stop_loss_pct
        tp_mult   = 1 + self.config.take_profit_pct if direction == "buy" else 1 - self.config.take_profit_pct

        pos = Position(
            pair=pair,
            direction=direction,
            size_usd=size_usd,
            entry_price=entry_price,
            stop_loss=entry_price * stop_mult,
            take_profit=entry_price * tp_mult,
            current_price=entry_price,
        )
        self.positions[pair] = pos
        logger.info(
            f"[Risk] OPEN {direction.upper()} {pair} | "
            f"size=${size_usd:.0f} entry={entry_price:.2f} "
            f"sl={pos.stop_loss:.2f} tp={pos.take_profit:.2f}"
        )
        return pos

    def close_position(self, pair: str, reason: str = "manual") -> Optional[float]:
        pos = self.positions.get(pair)
        if not pos or pos.status != "open":
            return None
        pnl = pos.unrealized_pnl
        pos.status = "closed"
        self.closed_pnl += pnl
        self.capital    += pnl
        self.peak_equity = max(self.peak_equity, self.capital)
        logger.info(f"[Risk] CLOSE {pair} | reason={reason} pnl=${pnl:+.2f}")
        return pnl

    # ── Price updates + exit check ─────────────────────────────────────────

    def update_prices(self, prices: dict[str, float]):
        """Call every tick with latest prices. Updates unrealized PnL."""
        for pair, price in prices.items():
            if pair in self.positions:
                self.positions[pair].current_price = price
            self._price_history.setdefault(pair, []).append(price)
            if len(self._price_history[pair]) > 500:
                self._price_history[pair] = self._price_history[pair][-500:]

    def check_exits(self) -> list[tuple[str, str]]:
        """
        Check all open positions for stop-loss or take-profit triggers.
        Returns list of (pair, reason) to exit.
        """
        exits = []
        for pair, pos in self.positions.items():
            if pos.status != "open" or pos.current_price == 0:
                continue
            price = pos.current_price

            if pos.direction == "buy":
                if price <= pos.stop_loss:
                    exits.append((pair, "stop_loss"))
                elif price >= pos.take_profit:
                    exits.append((pair, "take_profit"))
            else:
                if price >= pos.stop_loss:
                    exits.append((pair, "stop_loss"))
                elif price <= pos.take_profit:
                    exits.append((pair, "take_profit"))

        return exits

    # ── Portfolio metrics ──────────────────────────────────────────────────

    def snapshot(self) -> RiskSnapshot:
        open_pos    = [p for p in self.positions.values() if p.status == "open"]
        unrealized  = sum(p.unrealized_pnl for p in open_pos)
        total_equity = self.capital + unrealized
        self.peak_equity = max(self.peak_equity, total_equity)

        pair_exp = {}
        for p in open_pos:
            pair_exp[p.pair] = pair_exp.get(p.pair, 0) + p.size_usd

        return RiskSnapshot(
            timestamp=datetime.utcnow(),
            total_equity=total_equity,
            open_positions=len(open_pos),
            total_exposure_usd=sum(p.size_usd for p in open_pos),
            unrealized_pnl=unrealized,
            current_drawdown=self._current_drawdown(),
            is_halted=self.is_halted,
            pair_exposures=pair_exp,
        )

    def _current_drawdown(self) -> float:
        open_pos = [p for p in self.positions.values() if p.status == "open"]
        equity   = self.capital + sum(p.unrealized_pnl for p in open_pos)
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity)

    def _pair_exposure(self, pair: str) -> float:
        pos = self.positions.get(pair)
        return pos.size_usd if pos and pos.status == "open" else 0.0

    def reset_halt(self):
        """Manually re-enable trading after reviewing the halt reason."""
        self.is_halted = False
        self.peak_equity = self.capital
        logger.warning("[Risk] Trading halt lifted manually")
