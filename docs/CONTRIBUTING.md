# Contributing to Agenopoly

Thank you for your interest! Here's how to get involved.

## Development Setup

```bash
# 1. Clone and install
git clone https://github.com/YOUR_USERNAME/Agenopoly.git
cd Agenopoly
npm install
pip install -r requirements.txt

# 2. Copy env file
cp .env.example .env
# Fill in at minimum: ARB_SEPOLIA_RPC and ANTHROPIC_API_KEY

# 3. Start a local Arbitrum fork
npx hardhat node --fork https://arb1.arbitrum.io/rpc

# 4. Run contract tests
npx hardhat test

# 5. Run agent unit tests
pytest tests/unit/ -v
```

## Project Structure

- **agents/** — Python agent logic (no blockchain calls here)
- **contracts/** — Solidity contracts and interfaces
- **backtesting/** — Historical simulation engine
- **data_pipeline/** — Market data ingestion
- **tests/** — Unit and integration tests

## Coding Standards

- Python: follow PEP 8, type hints on all public functions
- Solidity: NatSpec comments on all public functions, events for all state changes
- All new features need tests before merging

## Opening a PR

1. Fork the repo and create a feature branch: `git checkout -b feat/your-feature`
2. Write tests for your changes
3. Ensure `npx hardhat test` and `pytest tests/` both pass
4. Open a PR with a clear description of what changes and why

## Reporting Issues

Open a GitHub issue with:
- Expected behavior
- Actual behavior
- Steps to reproduce
- Network/environment info
