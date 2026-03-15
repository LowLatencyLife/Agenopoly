"""
NegotiatorAgent — A trading agent that actively uses the A2A protocol.

Combines market signals (from MarketAnalystAgent) with on-chain negotiation:
  1. Generates trade signals every tick
  2. Decides whether to execute directly on DEX or negotiate via A2A
  3. Posts open proposals or targets specific counterparties
  4. Automatically polls order book and accepts matching proposals
  5. Schedules batch settlements to save gas
"""

import asyncio
import logging
from dataclasses import dataclass

from agents.base.agent import BaseAgent, AgentConfig, TradeSignal
from agents.a2a.client import A2AClient, ProposalParams
from agents.market_analyst.analyst import MarketAnalystAgent

logger = logging.getLogger(__name__)

# Arbitrum One token addresses
WETH_ARB  = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC_ARB  = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WBTC_ARB  = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0F"
ARB_TOKEN = "0x912CE59144191C1204E64559FE8253a0e49E6548"

# Minimum confidence to prefer A2A over direct DEX execution
A2A_CONFIDENCE_THRESHOLD = 0.72

# Slippage tolerance: 0.3% for direct DEX, 0.5% for A2A (extra buffer for latency)
SLIPPAGE_DEX = 0.003
SLIPPAGE_A2A = 0.005


@dataclass
class NegotiatorConfig:
    agent_config:        AgentConfig
    coordinator_address: str
    use_flashbots:       bool = True
    batch_interval_s:    int  = 120    # collect proposals then batch every 2 min
    poll_interval_s:     int  = 30     # check open order book every 30s


class NegotiatorAgent(BaseAgent):
    """
    Full A2A-capable agent. Inherits BaseAgent lifecycle and adds on-chain negotiation.
    """

    def __init__(self, config: NegotiatorConfig, anthropic_api_key: str = None):
        super().__init__(config.agent_config)
        self.neg_config = config
        self._analyst   = MarketAnalystAgent(config.agent_config, anthropic_api_key)

        self._a2a: A2AClient = None          # initialized on start (needs live w3)
        self._pending_batch: list[int] = []  # proposal IDs queued for batch execution
        self._accepted_proposals: list[int] = []

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self, tick_interval_seconds: int = 60):
        self._a2a = A2AClient(
            w3=self.w3,
            account=self.account,
            coordinator_address=self.neg_config.coordinator_address,
            use_flashbots=self.neg_config.use_flashbots,
        )
        logger.info(f"[{self.config.name}] A2A client initialized")

        # Run tick loop + background tasks concurrently
        await asyncio.gather(
            super().start(tick_interval_seconds),
            self._poll_loop(),
            self._batch_loop(),
        )

    # ── Signal generation (delegates to analyst) ───────────────────────────

    async def generate_signals(self) -> list[TradeSignal]:
        return await self._analyst.generate_signals()

    async def evaluate_proposal(self, proposal: dict) -> bool:
        return await self._analyst.evaluate_proposal(proposal)

    # ── Override: route signal to A2A or DEX ──────────────────────────────

    async def _handle_signal(self, signal: TradeSignal):
        if signal.confidence >= A2A_CONFIDENCE_THRESHOLD:
            await self._route_a2a(signal)
        else:
            await self._execute_dex_trade(signal)

    async def _route_a2a(self, signal: TradeSignal):
        """
        Decide Direct vs Open proposal based on available counterparties.
        Strategy: try Direct first (better price discovery), fallback to Open.
        """
        token_in, token_out = self._resolve_tokens(signal)
        amount_in      = int(signal.size_usd * 1e6)   # USDC 6 decimals
        min_amount_out = int(amount_in * (1 - SLIPPAGE_A2A))

        params = ProposalParams(
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            min_amount_out=min_amount_out,
        )

        # Look for a good counterparty in the open book first
        open_ids = await self._find_matching_open_proposals(signal)

        if open_ids:
            # Accept an existing open proposal — saves gas on creation
            proposal_id = open_ids[0]
            await self._a2a.accept(proposal_id)
            self._accepted_proposals.append(proposal_id)
            logger.info(f"[{self.config.name}] Accepted open proposal #{proposal_id} | {signal.pair}")
        else:
            # Post a new open proposal to the book
            proposal_id = await self._a2a.propose_open(params)
            logger.info(f"[{self.config.name}] Posted open proposal #{proposal_id} | {signal.pair}")

    async def _find_matching_open_proposals(self, signal: TradeSignal) -> list[int]:
        """Check the open order book for proposals that match our signal direction."""
        _, token_out = self._resolve_tokens(signal)
        try:
            return await self._a2a.poll_and_accept(
                wanted_token_out=token_out,
                min_amount_out=int(signal.size_usd * 0.95 * 1e6),
                evaluator_fn=self._evaluate_open_proposal,
            )
        except Exception as e:
            logger.warning(f"[{self.config.name}] poll_and_accept failed: {e}")
            return []

    async def _evaluate_open_proposal(self, proposal) -> bool:
        """Custom logic to decide whether to accept an open proposal."""
        # Basic checks: not expired, min size, reputation (registry enforces min but we add our own)
        if proposal.amount_in < 100 * 1e6:   # min $100
            return False
        return True

    # ── Background: poll open book ──────────────────────────────────────────

    async def _poll_loop(self):
        """Periodically check the open order book for proposals to accept."""
        while self.is_running:
            await asyncio.sleep(self.neg_config.poll_interval_s)
            try:
                # Poll for any WETH→USDC or USDC→WETH opportunities
                for token_out in [USDC_ARB, WETH_ARB]:
                    accepted = await self._a2a.poll_and_accept(
                        wanted_token_out=token_out,
                        min_amount_out=int(50 * 1e6),
                        evaluator_fn=self._evaluate_open_proposal,
                    )
                    if accepted:
                        self._pending_batch.extend(accepted)
            except Exception as e:
                logger.error(f"[{self.config.name}] Poll loop error: {e}")

    # ── Background: batch settlement ────────────────────────────────────────

    async def _batch_loop(self):
        """Every batch_interval_s, settle all pending accepted proposals in one tx."""
        while self.is_running:
            await asyncio.sleep(self.neg_config.batch_interval_s)
            if not self._pending_batch:
                continue
            batch = list(self._pending_batch)
            self._pending_batch.clear()
            try:
                count = await self._a2a.execute_batch(batch)
                logger.info(f"[{self.config.name}] Batch settled {count}/{len(batch)} proposals")
            except Exception as e:
                logger.error(f"[{self.config.name}] Batch execution failed: {e}")
                # Re-queue for next attempt
                self._pending_batch.extend(batch)

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_tokens(signal: TradeSignal) -> tuple[str, str]:
        """Map pair string to on-chain token addresses."""
        pair_map = {
            "WETH/USDC": (WETH_ARB, USDC_ARB),
            "USDC/WETH": (USDC_ARB, WETH_ARB),
            "WBTC/USDC": (WBTC_ARB, USDC_ARB),
            "ARB/USDC":  (ARB_TOKEN, USDC_ARB),
        }
        return pair_map.get(signal.pair, (WETH_ARB, USDC_ARB))
