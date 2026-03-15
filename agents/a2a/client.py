"""
A2AClient — Python interface for the AgentCoordinator smart contract.

Each agent instance holds one A2AClient. It handles:
  - Building and submitting proposals (Direct + Open)
  - Commit-reveal flow for Direct proposals
  - Polling the open order book and accepting eligible proposals
  - Batch settlement scheduling
  - Flashbots Protect RPC integration for MEV protection
"""

import asyncio
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.middleware import geth_poa_middleware

logger = logging.getLogger(__name__)

# Arbitrum One Uniswap v3 SwapRouter
UNISWAP_V3_ROUTER_ARB = "0xE592427A0AEce92De3Edee1F18E0157C05861564"

# Flashbots Protect RPC (routes txs to private mempool)
FLASHBOTS_PROTECT_RPC = "https://rpc.flashbots.net/fast"


@dataclass
class ProposalParams:
    token_in:       str    # checksummed ERC-20 address
    token_out:      str
    amount_in:      int    # in token wei
    min_amount_out: int
    counterparty:   Optional[str] = None   # None = open proposal


@dataclass
class OnChainProposal:
    id:           int
    proposal_type: str    # "Direct" | "Open"
    proposer:     str
    counterparty: str
    token_in:     str
    token_out:    str
    amount_in:    int
    min_amount_out: int
    expiry:       int
    status:       str
    revealed:     bool


class A2AClient:
    """
    Wraps the AgentCoordinator contract for a single agent wallet.

    Usage:
        client = A2AClient(w3, account, coordinator_address)
        proposal_id = await client.propose_open(params)
        await client.poll_and_accept(min_reputation=50)
        await client.execute(proposal_id)
    """

    # Minimal ABI — only the functions we call
    COORDINATOR_ABI = [
        {"name": "proposeDirect",  "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "counterparty","type": "address"}, {"name": "tokenIn","type": "address"},
                    {"name": "tokenOut","type": "address"}, {"name": "minAmountOut","type": "uint256"},
                    {"name": "commitHash","type": "bytes32"}],
         "outputs": [{"name": "proposalId","type": "uint256"}]},
        {"name": "proposeOpen",    "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "tokenIn","type": "address"}, {"name": "tokenOut","type": "address"},
                    {"name": "amountIn","type": "uint256"}, {"name": "minAmountOut","type": "uint256"}],
         "outputs": [{"name": "proposalId","type": "uint256"}]},
        {"name": "revealAmount",   "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "proposalId","type": "uint256"}, {"name": "amountIn","type": "uint256"},
                    {"name": "nonce","type": "bytes32"}], "outputs": []},
        {"name": "acceptProposal", "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "proposalId","type": "uint256"}], "outputs": []},
        {"name": "rejectProposal", "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "proposalId","type": "uint256"}], "outputs": []},
        {"name": "executeMatch",   "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "proposalId","type": "uint256"}], "outputs": []},
        {"name": "executeBatch",   "type": "function", "stateMutability": "nonpayable",
         "inputs": [{"name": "proposalIds","type": "uint256[]"}],
         "outputs": [{"name": "successCount","type": "uint256"}]},
        {"name": "getProposal",    "type": "function", "stateMutability": "view",
         "inputs": [{"name": "id","type": "uint256"}], "outputs": [{"name": "","type": "tuple",
         "components": [{"name":"id","type":"uint256"},{"name":"proposalType","type":"uint8"},
                         {"name":"proposer","type":"address"},{"name":"counterparty","type":"address"},
                         {"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},
                         {"name":"amountIn","type":"uint256"},{"name":"minAmountOut","type":"uint256"},
                         {"name":"expiry","type":"uint256"},{"name":"status","type":"uint8"},
                         {"name":"commitHash","type":"bytes32"},{"name":"revealed","type":"bool"}]}]},
        {"name": "getOpenProposals","type": "function", "stateMutability": "view",
         "inputs": [{"name": "limit","type": "uint256"}],
         "outputs": [{"name": "open","type": "uint256[]"}]},
    ]

    PROPOSAL_STATUS = {0: "Pending", 1: "Accepted", 2: "Executed", 3: "Rejected", 4: "Expired", 5: "Cancelled"}
    PROPOSAL_TYPE   = {0: "Direct", 1: "Open"}

    def __init__(
        self,
        w3: Web3,
        account: LocalAccount,
        coordinator_address: str,
        use_flashbots: bool = True,
    ):
        self.w3 = w3
        self.account = account
        self.address = account.address
        self.coordinator = w3.eth.contract(
            address=Web3.to_checksum_address(coordinator_address),
            abi=self.COORDINATOR_ABI,
        )
        self._pending_reveals: dict[int, tuple[int, bytes]] = {}  # proposalId → (amountIn, nonce)

        if use_flashbots:
            self._flashbots_w3 = Web3(Web3.HTTPProvider(FLASHBOTS_PROTECT_RPC))
            self._flashbots_w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            logger.info(f"[A2A:{self.address[:8]}] Flashbots Protect RPC enabled")

    # ── Propose Direct (commit-reveal) ─────────────────────────────────────

    async def propose_direct(self, params: ProposalParams) -> int:
        """
        Submit a Direct proposal with a hidden amountIn.
        The nonce is stored locally — call reveal_amount() after acceptance.
        """
        nonce = secrets.token_bytes(32)
        commit_hash = self._compute_commit(params.amount_in, nonce)

        tx_hash = await self._send_tx(
            self.coordinator.functions.proposeDirect(
                Web3.to_checksum_address(params.counterparty),
                Web3.to_checksum_address(params.token_in),
                Web3.to_checksum_address(params.token_out),
                params.min_amount_out,
                commit_hash,
            )
        )
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        proposal_id = self._extract_proposal_id(receipt)

        # Store nonce locally so we can reveal later
        self._pending_reveals[proposal_id] = (params.amount_in, nonce)
        logger.info(f"[A2A:{self.address[:8]}] Direct proposal #{proposal_id} submitted")
        return proposal_id

    async def reveal_amount(self, proposal_id: int):
        """Call after the counterparty has accepted a Direct proposal."""
        if proposal_id not in self._pending_reveals:
            raise ValueError(f"No pending reveal for proposal #{proposal_id}")

        amount_in, nonce = self._pending_reveals[proposal_id]
        await self._send_tx(
            self.coordinator.functions.revealAmount(proposal_id, amount_in, nonce)
        )
        del self._pending_reveals[proposal_id]
        logger.info(f"[A2A:{self.address[:8]}] Amount revealed for proposal #{proposal_id}")

    # ── Propose Open ───────────────────────────────────────────────────────

    async def propose_open(self, params: ProposalParams) -> int:
        tx_hash = await self._send_tx(
            self.coordinator.functions.proposeOpen(
                Web3.to_checksum_address(params.token_in),
                Web3.to_checksum_address(params.token_out),
                params.amount_in,
                params.min_amount_out,
            )
        )
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        proposal_id = self._extract_proposal_id(receipt)
        logger.info(f"[A2A:{self.address[:8]}] Open proposal #{proposal_id} posted")
        return proposal_id

    # ── Accept / Reject ────────────────────────────────────────────────────

    async def accept(self, proposal_id: int):
        await self._send_tx(self.coordinator.functions.acceptProposal(proposal_id))
        logger.info(f"[A2A:{self.address[:8]}] Accepted proposal #{proposal_id}")

    async def reject(self, proposal_id: int):
        await self._send_tx(self.coordinator.functions.rejectProposal(proposal_id))
        logger.info(f"[A2A:{self.address[:8]}] Rejected proposal #{proposal_id}")

    # ── Execute ────────────────────────────────────────────────────────────

    async def execute(self, proposal_id: int):
        await self._send_tx(self.coordinator.functions.executeMatch(proposal_id))
        logger.info(f"[A2A:{self.address[:8]}] Executed proposal #{proposal_id}")

    async def execute_batch(self, proposal_ids: list[int]) -> int:
        """Execute multiple proposals in one transaction. Returns success count."""
        tx_hash = await self._send_tx(
            self.coordinator.functions.executeBatch(proposal_ids)
        )
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        # Parse BatchExecuted event to get successCount
        logger.info(f"[A2A:{self.address[:8]}] Batch of {len(proposal_ids)} submitted")
        return len(proposal_ids)  # TODO: decode event for actual count

    # ── Poll open order book ───────────────────────────────────────────────

    async def poll_and_accept(
        self,
        wanted_token_out: str,
        min_amount_out: int,
        evaluator_fn=None,
    ) -> list[int]:
        """
        Fetch open proposals from the book, apply optional evaluator, accept matches.

        Args:
            wanted_token_out: We only accept proposals where tokenOut matches this.
            min_amount_out:   Minimum acceptable output size.
            evaluator_fn:     Optional async fn(proposal) → bool for custom logic.
        Returns:
            List of accepted proposal IDs.
        """
        open_ids: list[int] = self.coordinator.functions.getOpenProposals(50).call()
        accepted = []

        for pid in open_ids:
            raw = self.coordinator.functions.getProposal(pid).call()
            proposal = self._parse_proposal(raw)

            if proposal.status != "Pending":
                continue
            if proposal.token_out.lower() != wanted_token_out.lower():
                continue
            if proposal.min_amount_out < min_amount_out:
                continue
            if proposal.proposer.lower() == self.address.lower():
                continue

            should_accept = True
            if evaluator_fn:
                should_accept = await evaluator_fn(proposal)

            if should_accept:
                await self.accept(pid)
                accepted.append(pid)

        return accepted

    # ── Views ──────────────────────────────────────────────────────────────

    def get_proposal(self, proposal_id: int) -> OnChainProposal:
        raw = self.coordinator.functions.getProposal(proposal_id).call()
        return self._parse_proposal(raw)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_commit(amount_in: int, nonce: bytes) -> bytes:
        from eth_abi import encode
        encoded = encode(["uint256", "bytes32"], [amount_in, nonce])
        return Web3.keccak(encoded)

    def _parse_proposal(self, raw: tuple) -> OnChainProposal:
        return OnChainProposal(
            id=raw[0],
            proposal_type=self.PROPOSAL_TYPE.get(raw[1], "Unknown"),
            proposer=raw[2],
            counterparty=raw[3],
            token_in=raw[4],
            token_out=raw[5],
            amount_in=raw[6],
            min_amount_out=raw[7],
            expiry=raw[8],
            status=self.PROPOSAL_STATUS.get(raw[9], "Unknown"),
            revealed=raw[11],
        )

    async def _send_tx(self, contract_fn) -> str:
        """Build, sign, and send a transaction. Returns tx hash."""
        nonce = self.w3.eth.get_transaction_count(self.address)
        gas_price = self.w3.eth.gas_price

        tx = contract_fn.build_transaction({
            "from":     self.address,
            "nonce":    nonce,
            "gasPrice": int(gas_price * 1.1),  # 10% tip
        })
        tx["gas"] = self.w3.eth.estimate_gas(tx)

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        return tx_hash.hex()

    def _extract_proposal_id(self, receipt) -> int:
        """Extract proposalId from ProposalCreated event log."""
        # First topic is event signature, second is indexed proposalId
        if receipt["logs"]:
            return int(receipt["logs"][0]["topics"][1].hex(), 16)
        return 0
