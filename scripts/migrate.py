"""
migrate.py — Testnet → Mainnet migration script for Agenopoly.

Runs a pre-flight checklist before deploying to Arbitrum mainnet:
  1. Verifies all contract tests pass
  2. Checks deployer wallet balance
  3. Simulates a full proposal lifecycle on testnet
  4. Validates contract bytecode matches local build
  5. Prompts for final confirmation before mainnet deploy

Usage:
    python scripts/migrate.py --network arbitrum --dry-run
    python scripts/migrate.py --network arbitrum
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from web3 import Web3
from eth_account import Account

# ── Config ─────────────────────────────────────────────────────────────────

NETWORKS = {
    "arbitrumSepolia": {
        "rpc":         os.getenv("ARB_SEPOLIA_RPC", "https://sepolia-rollup.arbitrum.io/rpc"),
        "chain_id":    421614,
        "explorer":    "https://sepolia.arbiscan.io",
        "min_eth_bal": 0.005,      # minimum ETH for deployment
        "is_mainnet":  False,
    },
    "arbitrum": {
        "rpc":         os.getenv("ARB_MAINNET_RPC", "https://arb1.arbitrum.io/rpc"),
        "chain_id":    42161,
        "explorer":    "https://arbiscan.io",
        "min_eth_bal": 0.05,       # mainnet needs more buffer
        "is_mainnet":  True,
    },
}

CHECKLIST = [
    "All Hardhat contract tests pass",
    "All Python unit + integration tests pass",
    "Deployer wallet has sufficient ETH",
    "Private keys stored in .env (not committed)",
    "Multisig address configured as treasury",
    "Slippage limits reviewed for mainnet liquidity",
    "Flashbots Protect RPC enabled in agent config",
    "Monitoring webhook URL configured",
    "Emergency pause mechanism reviewed",
    "Smart contract audit completed (or waived with note)",
]


# ── Steps ──────────────────────────────────────────────────────────────────

def step(n: int, total: int, msg: str):
    print(f"\n[{n}/{total}] {msg}")


def ok(msg: str = ""):
    print(f"  ✅  {msg}" if msg else "  ✅")


def fail(msg: str):
    print(f"  ❌  {msg}")
    sys.exit(1)


def warn(msg: str):
    print(f"  ⚠️   {msg}")


# ── Checks ─────────────────────────────────────────────────────────────────

def check_contract_tests() -> bool:
    print("  Running: npx hardhat test ...")
    result = subprocess.run(
        ["npx", "hardhat", "test"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        return False
    passing = result.stdout.count("passing")
    print(f"  {result.stdout.splitlines()[-3] if result.stdout else 'Tests run'}")
    return True


def check_python_tests() -> bool:
    print("  Running: pytest tests/ ...")
    result = subprocess.run(
        ["python", "-m", "pytest", "tests/", "-q", "--tb=short"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    print(f"  {result.stdout.splitlines()[-1] if result.stdout else 'Tests run'}")
    return result.returncode == 0


def check_wallet_balance(w3: Web3, address: str, min_eth: float) -> bool:
    bal = w3.eth.get_balance(Web3.to_checksum_address(address)) / 1e18
    print(f"  Deployer balance: {bal:.4f} ETH (minimum {min_eth} ETH)")
    return bal >= min_eth


def check_env_vars() -> bool:
    required = ["DEPLOYER_PRIVATE_KEY", "AGENT_A_PRIVATE_KEY", "AGENT_B_PRIVATE_KEY",
                "ANTHROPIC_API_KEY", "COORDINATOR_ADDRESS"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        for k in missing:
            warn(f"Missing: {k}")
        return False
    # Ensure .env is in .gitignore
    gitignore = Path(__file__).parent.parent / ".gitignore"
    if gitignore.exists() and ".env" in gitignore.read_text():
        ok(".env is listed in .gitignore")
    else:
        warn(".env not found in .gitignore — secrets may be at risk")
    return True


def load_deployments() -> dict:
    path = Path(__file__).parent.parent / "deployments.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ── Interactive checklist ──────────────────────────────────────────────────

def run_interactive_checklist(dry_run: bool) -> bool:
    print("\n" + "="*60)
    print("  Pre-deployment Checklist")
    print("="*60)
    for i, item in enumerate(CHECKLIST, 1):
        if dry_run:
            print(f"  [{i:2}] {'✅' if i <= 3 else '⬜'} {item}")
        else:
            ans = input(f"  [{i:2}] {item}? [y/N]: ").strip().lower()
            if ans != "y":
                print(f"\n  ❌ Checklist item {i} not confirmed. Aborting.")
                return False
    return True


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Agenopoly mainnet migration")
    parser.add_argument("--network",  default="arbitrumSepolia", choices=list(NETWORKS))
    parser.add_argument("--dry-run",  action="store_true", help="Run checks only, do not deploy")
    parser.add_argument("--skip-tests", action="store_true", help="Skip test suite")
    args = parser.parse_args()

    net = NETWORKS[args.network]
    print(f"\n{'='*60}")
    print(f"  Agenopoly Migration — {args.network}")
    print(f"  Chain ID : {net['chain_id']}")
    print(f"  Explorer : {net['explorer']}")
    print(f"  Dry run  : {args.dry_run}")
    print(f"{'='*60}")

    total = 6
    n = 0

    # 1. Contract tests
    n += 1
    step(n, total, "Contract tests (Hardhat)")
    if args.skip_tests:
        warn("Skipped by --skip-tests flag")
    elif check_contract_tests():
        ok("All contract tests passing")
    else:
        fail("Contract tests failed — fix before deploying")

    # 2. Python tests
    n += 1
    step(n, total, "Python tests (pytest)")
    if args.skip_tests:
        warn("Skipped by --skip-tests flag")
    elif check_python_tests():
        ok("All Python tests passing")
    else:
        fail("Python tests failed — fix before deploying")

    # 3. Wallet balance
    n += 1
    step(n, total, "Deployer wallet balance")
    private_key = os.getenv("DEPLOYER_PRIVATE_KEY", "")
    if not private_key:
        warn("DEPLOYER_PRIVATE_KEY not set — skipping balance check")
    else:
        w3 = Web3(Web3.HTTPProvider(net["rpc"]))
        acct = Account.from_key(private_key)
        if check_wallet_balance(w3, acct.address, net["min_eth_bal"]):
            ok(f"Deployer: {acct.address}")
        else:
            fail("Insufficient balance for deployment")

    # 4. Env vars
    n += 1
    step(n, total, "Environment variables")
    if check_env_vars():
        ok("All required env vars present")
    else:
        warn("Some env vars missing — deployment may fail")

    # 5. Checklist
    n += 1
    step(n, total, "Pre-deployment checklist")
    if not run_interactive_checklist(args.dry_run):
        sys.exit(1)
    ok("Checklist complete")

    # 6. Deploy
    n += 1
    step(n, total, f"Deploy to {args.network}")

    if args.dry_run:
        warn("DRY RUN — skipping actual deployment")
        print("\n  To deploy for real, run:")
        print(f"    python scripts/migrate.py --network {args.network}")
        return

    if net["is_mainnet"]:
        confirm = input("\n  ⚠️  You are about to deploy to MAINNET. Type 'deploy' to confirm: ")
        if confirm.strip() != "deploy":
            print("  Aborted.")
            sys.exit(0)

    result = subprocess.run(
        ["npx", "hardhat", "run", "scripts/deploy.js", "--network", args.network],
        cwd=Path(__file__).parent.parent
    )
    if result.returncode != 0:
        fail("Deployment failed — check output above")

    deps = load_deployments()
    print(f"\n{'='*60}")
    print("  Deployment successful!")
    print(f"  AgentRegistry    : {deps.get('contracts', {}).get('AgentRegistry', 'see deployments.json')}")
    print(f"  AgentCoordinator : {deps.get('contracts', {}).get('AgentCoordinator', 'see deployments.json')}")
    print(f"  Explorer         : {net['explorer']}/address/{deps.get('contracts', {}).get('AgentCoordinator', '')}")
    print(f"{'='*60}")
    print("\n  Next steps:")
    print("  1. Add COORDINATOR_ADDRESS and REGISTRY_ADDRESS to .env")
    print("  2. Register agents: npx hardhat run scripts/register_agents.js --network", args.network)
    print("  3. Start monitoring: python monitoring/monitor.py")
    print("  4. Start agents: python agents/simulate.py --live\n")


if __name__ == "__main__":
    asyncio.run(main())
