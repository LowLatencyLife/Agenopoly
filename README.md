# 🤖 Agenopoly

> Autonomous crypto trading agents that negotiate with each other on-chain.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![CI](https://img.shields.io/badge/CI-GitHub%20Actions-green.svg)
![Network](https://img.shields.io/badge/network-Arbitrum%20One-purple.svg)
![Status](https://img.shields.io/badge/status-in%20development-yellow.svg)

---

## What is Agenopoly?

Agenopoly is a multi-agent crypto trading system where autonomous AI agents:

- **Analyze** markets using TA/FA signals and LLM-powered sentiment (Claude API)
- **Negotiate** trade proposals with each other via on-chain smart contracts
- **Execute** trades on Uniswap v3 (Arbitrum) with MEV protection
- **Build reputation** through a transparent on-chain scoring system

Each agent operates with its own wallet, risk parameters, and strategy. Agents post proposals to an open order book or target specific counterparties directly. A commit-reveal scheme keeps trade sizes hidden until both parties agree, preventing front-running.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       Agenopoly Network                          │
│                                                                  │
│  ┌───────────────┐  A2A Protocol  ┌──────────────────────────┐  │
│  │  NegotiatorA  │◄──────────────►│      NegotiatorB         │  │
│  │  (Trend)      │                │      (Arbitrage)         │  │
│  └───────┬───────┘                └────────────┬─────────────┘  │
│          │                                     │                 │
│          └──────────────┬──────────────────────┘                 │
│                         ▼                                        │
│              ┌──────────────────────┐                            │
│              │  AgentCoordinator    │                            │
│              │  (Arbitrum One)      │                            │
│              │  · Direct proposals  │                            │
│              │  · Open order book   │                            │
│              │  · Batch settlement  │                            │
│              └──────────┬───────────┘                            │
│                         ▼                                        │
│           Uniswap v3 · Flashbots Protect RPC                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## Phases

### Phase 1 — Base Infrastructure ✅
- CEX + on-chain data pipeline (Binance WebSocket, The Graph)
- Local simulation environment (Hardhat/Anvil fork)
- `BaseAgent` class with lifecycle, risk controls, and reputation

### Phase 2 — Signal Engine + Backtesting ✅
- 7 TA indicators: RSI, MACD, Bollinger Bands, EMA cross, ATR, OBV, volume spike
- LLM sentiment via Claude API with 15-minute cache
- Weighted voting aggregation — ATR scales all signals down in volatile regimes
- Risk Manager: fractional Kelly sizing, stop-loss/take-profit, drawdown circuit breaker
- BacktestEngine v2: Uniswap v3 price impact model, Arbitrum gas model, walk-forward k-fold validation
- Metrics: Sharpe, Sortino, Calmar, profit factor, max drawdown

### Phase 3 — A2A On-Chain Protocol ✅
- Three execution paths: Direct (commit-reveal), Open (order book), Batch (~40% gas saving)
- MEV protection: Flashbots Protect RPC + `minAmountOut` on every swap
- Reputation slashing: -1 on rejection, -5 on failed execution
- `AgentRegistry` — on-chain source of truth for agent status, rep, and trade stats

### Phase 4 — Production & Hardening ✅
- 15 Hardhat contract tests (AgentRegistry + AgentCoordinator, all paths + edge cases)
- 24/7 monitoring with heartbeat, on-chain event listener, and Slack/Discord webhook alerts
- `migrate.py` — testnet → mainnet pre-flight checklist with interactive confirmation
- `register_agents.js` — post-deploy agent wallet registration
- GitHub Actions CI: contract tests + Python tests + lint on every push
- Security audit checklist (`docs/SECURITY_AUDIT.md`) with 30+ items
- Mock contracts for isolated testing (ERC20Mock, SwapRouterMock)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Smart Contracts | Solidity 0.8.20, Hardhat, OpenZeppelin 5.x |
| Agent Runtime | Python 3.11+, asyncio, web3.py |
| Data | Binance WebSocket, The Graph (GraphQL) |
| DEX | Uniswap v3 (Arbitrum One) |
| LLM Signals | Claude API (Anthropic) |
| Network | Arbitrum One — ~100x cheaper gas than mainnet Ethereum |
| MEV Protection | Flashbots Protect RPC |
| CI | GitHub Actions |

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/YOUR_USERNAME/Agenopoly.git
cd Agenopoly
npm install
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Fill in: DEPLOYER_PRIVATE_KEY, AGENT_A_PRIVATE_KEY, AGENT_B_PRIVATE_KEY,
#          ANTHROPIC_API_KEY, ARB_MAINNET_RPC

# 3. Run all tests
npx hardhat test
python -m pytest tests/ -v

# 4. Local development (Arbitrum fork)
npx hardhat node --fork https://arb1.arbitrum.io/rpc
npx hardhat run scripts/deploy.js --network localhost

# 5. Testnet deployment
python scripts/migrate.py --network arbitrumSepolia --dry-run
python scripts/migrate.py --network arbitrumSepolia

# 6. Register agents after deploy
npx hardhat run scripts/register_agents.js --network arbitrumSepolia
```

---

## Project Structure

```
Agenopoly/
├── agents/
│   ├── base/              BaseAgent — lifecycle, risk, reputation
│   ├── market_analyst/    TA/FA signals + LLM sentiment
│   ├── risk_manager/      Kelly sizing, stop-loss, drawdown halt
│   └── a2a/               A2AClient, NegotiatorAgent
├── contracts/
│   ├── core/              AgentRegistry
│   ├── coordination/      AgentCoordinator (Direct/Open/Batch)
│   ├── interfaces/        IAgent, ISwapRouter
│   └── mocks/             ERC20Mock, SwapRouterMock (tests only)
├── backtesting/           BacktestEngine v2 + GasModel + SlippageModel
├── data_pipeline/         BinanceFeed, TheGraphFeed, DataPipeline
├── monitoring/            Monitor — heartbeat, chain events, alerts
├── scripts/
│   ├── deploy.js          Deploy all contracts
│   ├── register_agents.js Register agent wallets post-deploy
│   └── migrate.py         Testnet → mainnet pre-flight + deploy
├── tests/
│   ├── unit/              Python: indicators, signals, risk manager
│   ├── integration/       Python: backtest lifecycle, A2A protocol
│   └── contracts/         Hardhat: AgentRegistry, AgentCoordinator
├── docs/
│   ├── ARCHITECTURE.md    System design + data flow
│   ├── A2A_PROTOCOL.md    Sequence diagrams + MEV stack
│   ├── SECURITY_AUDIT.md  Pre-mainnet security checklist
│   └── CONTRIBUTING.md    Dev setup + standards
└── .github/workflows/     CI: tests + lint on every push
```

---

## Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| High A2A gas cost | Arbitrum One (~$0.12/swap) + batch settlement |
| Reentrancy | `ReentrancyGuard` on all state-changing functions |
| Oracle manipulation | Chainlink + Uniswap TWAP multi-source |
| MEV sandwich attacks | Flashbots Protect + `minAmountOut` |
| Backtest overfitting | Walk-forward k-fold validation |
| Agent key compromise | Separate deployer/agent/treasury wallets; minimal balances |

---

## Before Mainnet

See [`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md). Mandatory before go-live:

1. External smart contract audit
2. Minimum 7 days live on Arbitrum Sepolia
3. Treasury replaced with Gnosis Safe multisig
4. Initial capital cap ($500/agent for first 30 days)
5. Monitoring webhook active before launch

---

## Contributing

See [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE)
