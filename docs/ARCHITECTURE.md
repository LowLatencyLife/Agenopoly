# Agenopoly — Architecture

## System Overview

Agenopoly is a multi-layer system where AI agents trade autonomously and negotiate with each other using smart contracts as the coordination layer.

```
┌───────────────────────────────────────────────────────────────┐
│                         Agent Layer                           │
│                                                               │
│  BaseAgent                                                    │
│    ├── MarketAnalystAgent  (TA + LLM sentiment)               │
│    ├── ArbitrageAgent      (price discrepancies across DEXes) │
│    └── MarketMakerAgent    (liquidity provision, coming soon) │
└─────────────────────────┬─────────────────────────────────────┘
                          │ proposeMatch / acceptProposal
                          ▼
┌───────────────────────────────────────────────────────────────┐
│                    Coordination Layer (on-chain)              │
│                                                               │
│   AgentCoordinator.sol                                        │
│     - Proposal lifecycle (pending → accepted → executed)      │
│     - Reputation scoring (+2 success, -1 rejection)          │
│     - Anti-collusion: slash on failed execution               │
└─────────────────────────┬─────────────────────────────────────┘
                          │ executeMatch
                          ▼
┌───────────────────────────────────────────────────────────────┐
│                      Execution Layer                          │
│                                                               │
│   Uniswap v3 / Curve / Balancer                               │
│     - Actual token swaps                                      │
│     - MEV protection via Flashbots Protect RPC               │
└───────────────────────────────────────────────────────────────┘
```

## Agent-to-Agent (A2A) Protocol

### Proposal Lifecycle

```
Agent A                 AgentCoordinator               Agent B
   │                          │                           │
   │── proposeMatch() ────────►│                           │
   │                          │── ProposalCreated event ──►│
   │                          │                           │
   │                          │◄── acceptProposal() ───────│
   │◄── ProposalAccepted ──────│                           │
   │                          │                           │
   │── executeMatch() ────────►│                           │
   │                          │── swap tokens ────────────►│
   │◄── TradeExecuted ─────────│◄── TradeExecuted ──────────│
   │   (+2 reputation)        │                (+2 rep)    │
```

### Reputation System

| Event | Delta |
|---|---|
| Successful trade | +2 |
| Proposal rejected by counterparty | -1 |
| Failed execution (slashing) | -5 |
| Minimum to participate | 10 |

Reputation resets are prevented — scores accumulate over an agent's lifetime. This creates a long-term incentive for honest behavior.

## Data Flow

```
CEX WebSocket (Binance)          On-chain (Arbitrum)
        │                               │
        ▼                               ▼
   Price Feed                    The Graph Indexer
        │                               │
        └──────────┬────────────────────┘
                   ▼
           MarketAnalystAgent
                   │
            TA/FA signals
                   │
            LLM Sentiment
           (Claude API)
                   │
                   ▼
            Risk Manager
                   │
          passes risk check?
                  / \
                yes   no → discard
                 │
           proposeMatch()
          or DEX execution
```

## Network Choice: Arbitrum One

Mainnet Ethereum gas costs make frequent A2A proposals economically unviable (~$12/swap). Arbitrum reduces this by ~100x (~$0.12/swap), enabling:
- High-frequency proposal cycles without gas concerns
- Viable slippage model for small-to-medium position sizes
- Full EVM compatibility — contracts deploy unchanged from mainnet

## MEV Protection

Agent trade patterns, if predictable, can be exploited by MEV bots (sandwich attacks). Mitigations:
1. **Flashbots Protect RPC** — routes transactions to private mempool
2. **Slippage limits** — all trades set a `minAmountOut` on-chain
3. **Randomized timing** — agent tick intervals include jitter
4. **Private proposals** — targeted `counterparty` address prevents open observation

## Security Considerations

- `ReentrancyGuard` on all state-changing contract functions
- No single admin key controls funds; treasury is a multisig in production
- Agent private keys are never stored in contract state
- Oracle price sources use Chainlink + Uniswap TWAP for manipulation resistance
- All contracts should be audited before mainnet deployment
