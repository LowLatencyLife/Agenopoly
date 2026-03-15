# A2A Protocol — Technical Specification

## Overview

The Agent-to-Agent (A2A) protocol is the core of Agenopoly. It allows autonomous agents to negotiate and execute trades with each other on-chain, without any central intermediary.

## Three Proposal Paths

### Path 1 — Direct (commit-reveal)

Used when Agent A wants to trade specifically with Agent B, but doesn't want to reveal the trade size before B agrees.

```
Agent A                          Contract                     Agent B
  │                                  │                           │
  │  proposeDirect(B, commit_hash)   │                           │
  │─────────────────────────────────►│                           │
  │  ← proposalId                    │  onProposalReceived()     │
  │                                  │──────────────────────────►│
  │                                  │                           │ (evaluates signal)
  │                                  │   acceptProposal(id)      │
  │                                  │◄──────────────────────────│
  │  ← ProposalAccepted event        │                           │
  │                                  │                           │
  │  revealAmount(id, amount, nonce) │                           │
  │─────────────────────────────────►│                           │
  │                                  │                           │
  │  executeMatch(id)                │                           │
  │─────────────────────────────────►│  swap via Uniswap v3      │
  │                                  │─────────────────────────► │
  │  ← TradeExecuted (+2 rep each)   │                           │
```

**Why commit-reveal?** If Agent A broadcasts `amountIn = 500,000 USDC` before B accepts, MEV bots can front-run the swap on Uniswap. With commit-reveal, the size is only visible on-chain after both parties have committed, leaving no window for front-running.

### Path 2 — Open Order Book

Used when Agent A wants to find the best counterparty without targeting one specifically.

```
Agent A                         Contract                    Any Agent
  │                                 │                           │
  │  proposeOpen(WETH, USDC, 1e18) │                           │
  │────────────────────────────────►│                           │
  │                                 │  (published on-chain)     │
  │                                 │   getOpenProposals()      │
  │                                 │◄──────────────────────────│
  │                                 │   acceptProposal(id)      │
  │                                 │◄──────────────────────────│
  │  ← ProposalAccepted             │                           │
  │                                 │                           │
  │  executeMatch(id)               │── swap ──────────────────►│
  │────────────────────────────────►│                           │
```

Open proposals are visible to all registered agents. The first eligible agent to call `acceptProposal()` wins — gas speed matters here. This creates a competitive dynamic where fast/capable agents are rewarded.

### Path 3 — Batch Settlement

Instead of executing proposals one by one (each costing gas), agents accumulate accepted proposals and settle them in a single transaction.

```
Agent A accumulates proposals [id:12, id:15, id:19, id:22]
                │
                ▼
    executeBatch([12, 15, 19, 22])
                │
    ┌───────────┼───────────┐
    │           │           │
  swap 12    swap 15    swap 19   ← swap 22 fails → slashed
    │           │           │
    └───────────┼───────────┘
                ▼
    BatchExecuted(batchId=7, successCount=3)
```

Gas saving: ~40% per proposal compared to individual execution. This is significant on Arbitrum at scale.

## Reputation System

| Event | Rep delta | Notes |
|---|---|---|
| Trade executed successfully | +2 | Both proposer and counterparty |
| Proposal rejected by counterparty | -1 | Discourages spamming low-quality proposals |
| Failed execution (slashed) | -5 | Agent accepted but execution failed |
| Minimum to participate | ≥10 | Enforced by `AgentRegistry.canParticipate()` |
| Starting score | 100 | All newly registered agents |
| Maximum score | 1000 | Prevents runaway advantage |

Reputation is fully on-chain and transparent. Any agent can query `registry.getAgent(address)` to see another agent's score before engaging.

## MEV Protection Stack

Three layers of protection, from most to least effective:

**1. Flashbots Protect RPC (Python layer)**
Transactions are submitted to Flashbots' private mempool rather than the public one. MEV searchers cannot see them before inclusion. Activated by default in `A2AClient(use_flashbots=True)`.

**2. Commit-reveal on Direct proposals (contract layer)**
`amountIn` is hidden as a hash until counterparty accepts. Even if a searcher monitors `ProposeCreated` events, they don't know the trade size.

**3. `minAmountOut` on every swap (contract layer)**
All Uniswap v3 calls set `amountOutMinimum`. If a sandwich attack moves the price too far, the swap reverts automatically.

## Contract Addresses (Arbitrum One)

Fill in after deployment:

| Contract | Address |
|---|---|
| AgentRegistry | `deployments.json → contracts.AgentRegistry` |
| AgentCoordinator | `deployments.json → contracts.AgentCoordinator` |
| Uniswap v3 Router | `0xE592427A0AEce92De3Edee1F18E0157C05861564` |

## Running the Full Stack Locally

```bash
# Terminal 1: local Arbitrum fork
npx hardhat node --fork https://arb1.arbitrum.io/rpc

# Terminal 2: deploy contracts
npx hardhat run scripts/deploy.js --network localhost
# Copy COORDINATOR_ADDRESS and REGISTRY_ADDRESS to .env

# Terminal 3: run two negotiating agents
python -c "
import asyncio
from agents.a2a.negotiator import NegotiatorAgent, NegotiatorConfig
from agents.base.agent import AgentConfig

cfg_a = NegotiatorConfig(
    agent_config=AgentConfig(name='Alpha', strategy='TREND', private_key='0x...'),
    coordinator_address='0x...',
)
cfg_b = NegotiatorConfig(
    agent_config=AgentConfig(name='Beta', strategy='ARBITRAGE', private_key='0x...'),
    coordinator_address='0x...',
)

async def main():
    await asyncio.gather(
        NegotiatorAgent(cfg_a).start(tick_interval_seconds=30),
        NegotiatorAgent(cfg_b).start(tick_interval_seconds=30),
    )

asyncio.run(main())
"
```
