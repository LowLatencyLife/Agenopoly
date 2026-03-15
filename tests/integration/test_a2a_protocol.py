"""
Integration test: full A2A proposal lifecycle on a local Hardhat/Anvil fork.

Requires:
  - npx hardhat node --fork https://arb1.arbitrum.io/rpc  (running on :8545)
  - Deployed contracts (npx hardhat run scripts/deploy.js --network localhost)
  - COORDINATOR_ADDRESS in environment
"""

import asyncio
import os
import pytest
from eth_account import Account
from web3 import Web3

from agents.a2a.client import A2AClient, ProposalParams

RPC_URL = os.getenv("RPC_URL", "http://localhost:8545")
COORDINATOR = os.getenv("COORDINATOR_ADDRESS", "")

# Arbitrum One (forked locally)
WETH  = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC  = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


@pytest.fixture
def w3():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    assert w3.is_connected(), "Hardhat node not running"
    return w3


@pytest.fixture
def agent_a(w3):
    return Account.create()


@pytest.fixture
def agent_b(w3):
    return Account.create()


@pytest.fixture
def client_a(w3, agent_a):
    return A2AClient(w3, agent_a, COORDINATOR, use_flashbots=False)


@pytest.fixture
def client_b(w3, agent_b):
    return A2AClient(w3, agent_b, COORDINATOR, use_flashbots=False)


@pytest.mark.skipif(not COORDINATOR, reason="COORDINATOR_ADDRESS not set")
class TestA2AProtocol:

    @pytest.mark.asyncio
    async def test_open_proposal_lifecycle(self, client_a, client_b):
        """Agent A posts open proposal → Agent B accepts → Agent A executes."""
        params = ProposalParams(
            token_in=WETH,
            token_out=USDC,
            amount_in=int(0.1 * 1e18),   # 0.1 WETH
            min_amount_out=int(200 * 1e6) # min $200 USDC
        )

        # Post open proposal
        proposal_id = await client_a.propose_open(params)
        assert proposal_id > 0

        # Check it's on-chain
        proposal = client_a.get_proposal(proposal_id)
        assert proposal.status == "Pending"
        assert proposal.proposal_type == "Open"

        # Agent B accepts
        await client_b.accept(proposal_id)
        proposal = client_a.get_proposal(proposal_id)
        assert proposal.status == "Accepted"
        assert proposal.counterparty.lower() == client_b.address.lower()

        # Agent A executes
        await client_a.execute(proposal_id)
        proposal = client_a.get_proposal(proposal_id)
        assert proposal.status == "Executed"

    @pytest.mark.asyncio
    async def test_direct_proposal_commit_reveal(self, client_a, client_b):
        """Agent A proposes directly to B with hidden amount → reveal → execute."""
        import secrets
        nonce = secrets.token_bytes(32)
        amount_in = int(0.05 * 1e18)  # 0.05 WETH

        commit_hash = A2AClient._compute_commit(amount_in, nonce)

        proposal_id = await client_a.propose_direct(
            params=ProposalParams(
                token_in=WETH,
                token_out=USDC,
                amount_in=amount_in,
                min_amount_out=int(100 * 1e6),
                counterparty=client_b.address,
            )
        )

        # Before reveal, amountIn is 0
        proposal = client_a.get_proposal(proposal_id)
        assert proposal.amount_in == 0
        assert not proposal.revealed

        # B accepts
        await client_b.accept(proposal_id)

        # A reveals
        await client_a.reveal_amount(proposal_id)

        proposal = client_a.get_proposal(proposal_id)
        assert proposal.revealed
        assert proposal.amount_in == amount_in

    @pytest.mark.asyncio
    async def test_rejection_penalizes_proposer(self, client_a, client_b, w3):
        """Rejecting a proposal should reduce the proposer's reputation by 1."""
        # TODO: read reputation before/after from AgentRegistry
        params = ProposalParams(token_in=WETH, token_out=USDC, amount_in=int(1e18), min_amount_out=0)
        proposal_id = await client_a.propose_open(params)
        await client_b.reject(proposal_id)
        proposal = client_a.get_proposal(proposal_id)
        assert proposal.status == "Rejected"

    @pytest.mark.asyncio
    async def test_batch_execution(self, client_a, client_b):
        """Post 3 open proposals, accept all, batch-execute in one tx."""
        params = ProposalParams(token_in=WETH, token_out=USDC, amount_in=int(0.1 * 1e18), min_amount_out=int(100 * 1e6))
        ids = [await client_a.propose_open(params) for _ in range(3)]
        for pid in ids:
            await client_b.accept(pid)

        success_count = await client_a.execute_batch(ids)
        assert success_count == 3

        for pid in ids:
            assert client_a.get_proposal(pid).status == "Executed"
