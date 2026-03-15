"""
Monitor — 24/7 production monitoring for Agenopoly agents.

Tracks:
  - Agent heartbeat (last tick timestamp)
  - On-chain events: TradeExecuted, AgentSlashed, ProposalCreated
  - Portfolio PnL vs drawdown thresholds
  - Gas balance (warn when low)
  - Contract state (is coordinator paused?)

Alerts via:
  - Console logging (always)
  - Webhook (Slack / Discord / PagerDuty) — configured in .env
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from web3 import Web3

logger = logging.getLogger(__name__)

# ── Alert levels ───────────────────────────────────────────────────────────

class AlertLevel:
    INFO    = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class Alert:
    level:     str
    title:     str
    message:   str
    agent:     Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_slack_block(self) -> dict:
        icons = {AlertLevel.INFO: "ℹ️", AlertLevel.WARNING: "⚠️", AlertLevel.CRITICAL: "🚨"}
        return {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{icons[self.level]} {self.title}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": self.message}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": f"Agent: `{self.agent or 'system'}` | {self.timestamp.strftime('%Y-%m-%d %H:%M UTC')}"}
                ]},
            ]
        }


# ── Thresholds ─────────────────────────────────────────────────────────────

@dataclass
class MonitorConfig:
    rpc_url:              str
    coordinator_address:  str
    registry_address:     str
    webhook_url:          str = ""                  # Slack/Discord webhook
    heartbeat_timeout_s:  int = 180                 # alert if no tick in 3 min
    gas_warn_threshold_eth: float = 0.01            # warn if wallet < 0.01 ETH
    drawdown_warn_pct:    float = 0.08              # warn at 8% drawdown
    drawdown_halt_pct:    float = 0.15              # critical at 15%
    slashing_alert:       bool = True               # alert on any reputation slash
    poll_interval_s:      int = 30


# ── On-chain event listener ────────────────────────────────────────────────

COORDINATOR_ABI_EVENTS = [
    {"name": "TradeExecuted", "type": "event",
     "inputs": [
         {"name": "id",           "type": "uint256", "indexed": True},
         {"name": "proposer",     "type": "address", "indexed": True},
         {"name": "counterparty", "type": "address", "indexed": True},
         {"name": "amountIn",     "type": "uint256", "indexed": False},
         {"name": "amountOut",    "type": "uint256", "indexed": False},
     ]},
    {"name": "AgentSlashed", "type": "event",
     "inputs": [
         {"name": "agent",      "type": "address", "indexed": True},
         {"name": "proposalId", "type": "uint256", "indexed": False},
         {"name": "delta",      "type": "int256",  "indexed": False},
     ]},
    {"name": "BatchExecuted", "type": "event",
     "inputs": [
         {"name": "batchId",      "type": "uint256", "indexed": True},
         {"name": "successCount", "type": "uint256", "indexed": False},
     ]},
]


# ── Agent health record ────────────────────────────────────────────────────

@dataclass
class AgentHealth:
    name:            str
    address:         str
    last_tick:       datetime = field(default_factory=datetime.utcnow)
    total_trades:    int   = 0
    total_pnl_usd:   float = 0.0
    reputation:      int   = 100
    eth_balance:     float = 0.0
    is_halted:       bool  = False
    alerts:          list[Alert] = field(default_factory=list)


# ── Monitor ────────────────────────────────────────────────────────────────

class Monitor:
    """
    Long-running async monitor process. Runs alongside agents.

    Usage:
        monitor = Monitor(config, agents={"Alpha": "0xABCD...", "Beta": "0x1234..."})
        await monitor.start()
    """

    def __init__(self, config: MonitorConfig, agents: dict[str, str]):
        self.config  = config
        self.agents  = agents                        # {name: wallet_address}
        self.health  = {name: AgentHealth(name=name, address=addr) for name, addr in agents.items()}
        self.w3      = Web3(Web3.HTTPProvider(config.rpc_url))
        self._last_block = 0
        self._running = False

    async def start(self):
        self._running = True
        self._last_block = self.w3.eth.block_number
        logger.info(f"[Monitor] Starting from block {self._last_block}")
        await asyncio.gather(
            self._heartbeat_loop(),
            self._chain_event_loop(),
            self._balance_loop(),
        )

    async def stop(self):
        self._running = False

    # ── Heartbeat ──────────────────────────────────────────────────────────

    def record_tick(self, agent_name: str, pnl_delta: float = 0.0):
        """Called by each agent at the end of every tick."""
        h = self.health.get(agent_name)
        if h:
            h.last_tick     = datetime.utcnow()
            h.total_pnl_usd += pnl_delta

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(self.config.poll_interval_s)
            now = datetime.utcnow()
            for name, h in self.health.items():
                age = (now - h.last_tick).total_seconds()
                if age > self.config.heartbeat_timeout_s:
                    await self._send_alert(Alert(
                        level=AlertLevel.CRITICAL,
                        title=f"Agent {name} unresponsive",
                        message=f"No heartbeat for {age:.0f}s (threshold {self.config.heartbeat_timeout_s}s). Last tick: {h.last_tick.strftime('%H:%M:%S UTC')}",
                        agent=name,
                    ))

    # ── On-chain events ────────────────────────────────────────────────────

    async def _chain_event_loop(self):
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config.coordinator_address),
            abi=COORDINATOR_ABI_EVENTS,
        )
        while self._running:
            await asyncio.sleep(self.config.poll_interval_s)
            try:
                current = self.w3.eth.block_number
                if current <= self._last_block:
                    continue

                # TradeExecuted
                for evt in contract.events.TradeExecuted.get_logs(
                    fromBlock=self._last_block + 1, toBlock=current
                ):
                    await self._on_trade_executed(evt)

                # AgentSlashed
                if self.config.slashing_alert:
                    for evt in contract.events.AgentSlashed.get_logs(
                        fromBlock=self._last_block + 1, toBlock=current
                    ):
                        await self._on_agent_slashed(evt)

                # BatchExecuted
                for evt in contract.events.BatchExecuted.get_logs(
                    fromBlock=self._last_block + 1, toBlock=current
                ):
                    logger.info(f"[Monitor] Batch {evt.args.batchId}: {evt.args.successCount} trades settled")

                self._last_block = current

            except Exception as e:
                logger.error(f"[Monitor] Chain event loop error: {e}")

    async def _on_trade_executed(self, evt):
        args = evt.args
        agent_name = self._address_to_name(args.proposer) or args.proposer[:8]
        amount_out = args.amountOut / 1e6  # assume USDC 6 decimals
        logger.info(f"[Monitor] TradeExecuted #{args.id} | {agent_name} | out=${amount_out:.2f}")

        h = self.health.get(agent_name)
        if h:
            h.total_trades += 1

    async def _on_agent_slashed(self, evt):
        args = evt.args
        agent_name = self._address_to_name(args.agent) or args.agent[:8]
        await self._send_alert(Alert(
            level=AlertLevel.WARNING,
            title=f"Agent {agent_name} slashed",
            message=f"Reputation delta: {args.delta} on proposal #{args.proposalId}",
            agent=agent_name,
        ))

    # ── Balance check ──────────────────────────────────────────────────────

    async def _balance_loop(self):
        while self._running:
            await asyncio.sleep(self.config.poll_interval_s * 2)
            for name, addr in self.agents.items():
                try:
                    bal_wei = self.w3.eth.get_balance(Web3.to_checksum_address(addr))
                    bal_eth = bal_wei / 1e18
                    self.health[name].eth_balance = bal_eth
                    if bal_eth < self.config.gas_warn_threshold_eth:
                        await self._send_alert(Alert(
                            level=AlertLevel.WARNING,
                            title=f"Low gas balance — {name}",
                            message=f"Wallet `{addr[:10]}...` has {bal_eth:.4f} ETH (threshold {self.config.gas_warn_threshold_eth} ETH). Top up to avoid failed transactions.",
                            agent=name,
                        ))
                except Exception as e:
                    logger.debug(f"[Monitor] Balance check failed for {name}: {e}")

    # ── PnL / drawdown ─────────────────────────────────────────────────────

    def check_drawdown(self, agent_name: str, current_drawdown: float):
        """Call this from the agent's risk manager on each snapshot."""
        h = self.health.get(agent_name)
        if not h:
            return
        if current_drawdown >= self.config.drawdown_halt_pct:
            h.is_halted = True
            asyncio.create_task(self._send_alert(Alert(
                level=AlertLevel.CRITICAL,
                title=f"Drawdown HALT — {agent_name}",
                message=f"Drawdown {current_drawdown:.1%} exceeded halt threshold {self.config.drawdown_halt_pct:.1%}. Trading paused automatically.",
                agent=agent_name,
            )))
        elif current_drawdown >= self.config.drawdown_warn_pct:
            asyncio.create_task(self._send_alert(Alert(
                level=AlertLevel.WARNING,
                title=f"Drawdown warning — {agent_name}",
                message=f"Drawdown {current_drawdown:.1%} approaching halt threshold {self.config.drawdown_halt_pct:.1%}.",
                agent=agent_name,
            )))

    # ── Alerting ───────────────────────────────────────────────────────────

    async def _send_alert(self, alert: Alert):
        level_log = {
            AlertLevel.INFO:     logger.info,
            AlertLevel.WARNING:  logger.warning,
            AlertLevel.CRITICAL: logger.critical,
        }.get(alert.level, logger.info)

        level_log(f"[{alert.level}] {alert.title}: {alert.message}")

        h = self.health.get(alert.agent)
        if h:
            h.alerts.append(alert)

        if self.config.webhook_url:
            await self._post_webhook(alert)

    async def _post_webhook(self, alert: Alert):
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    self.config.webhook_url,
                    json=alert.to_slack_block(),
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception as e:
            logger.debug(f"[Monitor] Webhook delivery failed: {e}")

    # ── Dashboard ──────────────────────────────────────────────────────────

    def status_report(self) -> str:
        now = datetime.utcnow()
        lines = [f"\n{'='*60}", f"  Agenopoly Monitor — {now.strftime('%Y-%m-%d %H:%M UTC')}", f"{'='*60}"]
        for name, h in self.health.items():
            age  = (now - h.last_tick).total_seconds()
            status = "🟢 LIVE" if age < self.config.heartbeat_timeout_s else "🔴 DEAD"
            halt   = " [HALTED]" if h.is_halted else ""
            lines.append(
                f"  {status}{halt} {name}\n"
                f"    Wallet   : {h.address[:10]}...\n"
                f"    ETH bal  : {h.eth_balance:.4f} ETH\n"
                f"    Rep      : {h.reputation}\n"
                f"    Trades   : {h.total_trades}\n"
                f"    PnL      : ${h.total_pnl_usd:+,.2f}\n"
                f"    Last tick: {age:.0f}s ago"
            )
        lines.append(f"{'='*60}")
        return "\n".join(lines)

    def _address_to_name(self, address: str) -> Optional[str]:
        for name, addr in self.agents.items():
            if addr.lower() == address.lower():
                return name
        return None
