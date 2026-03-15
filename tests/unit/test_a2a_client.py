"""
Unit tests for A2AClient helpers — no blockchain required.
"""

import secrets
import pytest
from eth_account import Account
from unittest.mock import MagicMock
from web3 import Web3

from agents.a2a.client import A2AClient


@pytest.fixture
def mock_client():
    w3 = MagicMock()
    w3.eth.contract.return_value = MagicMock()
    account = Account.create()
    return A2AClient(w3, account, "0x" + "a" * 40, use_flashbots=False)


class TestCommitReveal:

    def test_commit_hash_is_deterministic(self):
        nonce = secrets.token_bytes(32)
        amount = 1_000_000
        h1 = A2AClient._compute_commit(amount, nonce)
        h2 = A2AClient._compute_commit(amount, nonce)
        assert h1 == h2

    def test_different_amounts_produce_different_hashes(self):
        nonce = secrets.token_bytes(32)
        h1 = A2AClient._compute_commit(1_000_000, nonce)
        h2 = A2AClient._compute_commit(2_000_000, nonce)
        assert h1 != h2

    def test_different_nonces_produce_different_hashes(self):
        amount = 500_000
        h1 = A2AClient._compute_commit(amount, secrets.token_bytes(32))
        h2 = A2AClient._compute_commit(amount, secrets.token_bytes(32))
        assert h1 != h2

    def test_commit_is_32_bytes(self):
        h = A2AClient._compute_commit(999, secrets.token_bytes(32))
        assert len(h) == 32


class TestProposalParsing:

    def test_parse_open_proposal(self, mock_client):
        raw = (
            1,           # id
            1,           # type = Open
            "0x" + "a"*40,  # proposer
            "0x" + "0"*40,  # counterparty (zero = open)
            "0x" + "b"*40,  # tokenIn
            "0x" + "c"*40,  # tokenOut
            1_000_000,   # amountIn
            900_000,     # minAmountOut
            9999999999,  # expiry
            0,           # status = Pending
            b'\x00' * 32,  # commitHash
            True,        # revealed (open proposals are always revealed)
        )
        p = mock_client._parse_proposal(raw)
        assert p.proposal_type == "Open"
        assert p.status == "Pending"
        assert p.revealed is True

    def test_parse_direct_proposal_unrevealed(self, mock_client):
        raw = (
            2, 0, "0x"+"a"*40, "0x"+"b"*40,
            "0x"+"c"*40, "0x"+"d"*40,
            0,            # amountIn = 0 (not yet revealed)
            500_000, 9999999999, 1,   # status = Accepted
            b'\xab' * 32, False,      # not revealed
        )
        p = mock_client._parse_proposal(raw)
        assert p.proposal_type == "Direct"
        assert p.amount_in == 0
        assert p.revealed is False
        assert p.status == "Accepted"
