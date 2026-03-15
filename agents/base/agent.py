"""
BaseAgent — Core class for all Agenopoly trading agents.
Every specialized agent (trend follower, arbitrage, market maker) inherits from this.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from eth_account import Account
from web3 import Web3

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    name: str
    strategy: str
    max_position_usd: float = 1000.0
    max_drawdown_pct: float = 0.10       # 10%
    stop_loss_pct: float = 0.05          # 5%
    min_reputation_score: int = 50       # min score to negotiate with
    rpc_url: str = "http://localhost:8545"
    private_key: Optional[str] = None


@dataclass
class TradeSignal:
    pair: str                            # e.g. "WETH/USDC"
    direction: str                       # "buy" | "sell"
    confidence: float                    # 0.0 – 1.0
    size_usd: float
    source: str                          # "TA" | "FA" | "A2A"
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class BaseAgent(ABC):
    """
    Abstract base class for all Agenopoly agents.

    Lifecycle:
        1. __init__  — load config, connect web3, register on-chain
        2. start()   — begin the async event loop
        3. tick()    — called every cycle; generate signals + execute
        4. stop()    — graceful shutdown
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        self.account = (
            Account.from_key(config.private_key)
            if config.private_key
            else Account.create()
        )
        self.wallet_address = self.account.address
        self.is_running = False
        self.reputation_score = 100
        self.pnl_usd = 0.0
        self.open_positions: dict = {}
        logger.info(f"[{self.config.name}] Initialized | wallet: {self.wallet_address}")

    # ── Abstract methods (must implement in subclass) ──────────────────────

    @abstractmethod
    async def generate_signals(self) -> list[TradeSignal]:
        """Analyze market data and return a list of trade signals."""
        ...

    @abstractmethod
    async def evaluate_proposal(self, proposal: dict) -> bool:
        """Evaluate an A2A trade proposal from another agent. Return True to accept."""
        ...

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self, tick_interval_seconds: int = 60):
        self.is_running = True
        logger.info(f"[{self.config.name}] Starting — tick every {tick_interval_seconds}s")
        while self.is_running:
            try:
                await self.tick()
            except Exception as e:
                logger.error(f"[{self.config.name}] Tick error: {e}", exc_info=True)
            await asyncio.sleep(tick_interval_seconds)

    async def stop(self):
        self.is_running = False
        logger.info(f"[{self.config.name}] Stopped | final PnL: ${self.pnl_usd:+.2f}")

    async def tick(self):
        """One full cycle: generate signals → risk check → propose or execute."""
        signals = await self.generate_signals()
        for signal in signals:
            if self._passes_risk_check(signal):
                await self._handle_signal(signal)

    # ── Risk management ────────────────────────────────────────────────────

    def _passes_risk_check(self, signal: TradeSignal) -> bool:
        if signal.size_usd > self.config.max_position_usd:
            logger.warning(f"[{self.config.name}] Signal rejected: size ${signal.size_usd} > max ${self.config.max_position_usd}")
            return False
        if self._current_drawdown() > self.config.max_drawdown_pct:
            logger.warning(f"[{self.config.name}] Signal rejected: drawdown limit reached")
            return False
        if signal.confidence < 0.5:
            logger.debug(f"[{self.config.name}] Signal rejected: low confidence {signal.confidence:.2f}")
            return False
        return True

    def _current_drawdown(self) -> float:
        if not self.open_positions:
            return 0.0
        unrealized = sum(p.get("unrealized_pnl", 0) for p in self.open_positions.values())
        peak = max(self.pnl_usd + unrealized, 0.01)
        return max(0.0, -unrealized / peak)

    # ── Signal execution ───────────────────────────────────────────────────

    async def _handle_signal(self, signal: TradeSignal):
        if signal.source == "A2A":
            await self._execute_a2a_trade(signal)
        else:
            await self._execute_dex_trade(signal)

    async def _execute_dex_trade(self, signal: TradeSignal):
        logger.info(
            f"[{self.config.name}] DEX trade | {signal.direction.upper()} "
            f"{signal.pair} ${signal.size_usd:.0f} (conf: {signal.confidence:.2f})"
        )
        # TODO: integrate Uniswap v3 SDK

    async def _execute_a2a_trade(self, signal: TradeSignal):
        logger.info(
            f"[{self.config.name}] A2A trade | {signal.direction.upper()} "
            f"{signal.pair} via agent {signal.metadata.get('counterparty', '?')}"
        )
        # TODO: call CoordinationContract.executeMatch()

    # ── Reputation ─────────────────────────────────────────────────────────

    def update_reputation(self, delta: int):
        self.reputation_score = max(0, min(100, self.reputation_score + delta))
        logger.debug(f"[{self.config.name}] Reputation: {self.reputation_score}")

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} name={self.config.name!r} "
            f"wallet={self.wallet_address[:8]}... "
            f"rep={self.reputation_score} pnl=${self.pnl_usd:+.2f}>"
        )
