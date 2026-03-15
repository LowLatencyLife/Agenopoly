// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "../core/AgentRegistry.sol";
import "../interfaces/IAgent.sol";
import "../interfaces/ISwapRouter.sol";

/**
 * @title  AgentCoordinator v2
 * @notice Full A2A on-chain protocol for Agenopoly.
 *
 *  Three execution paths:
 *   1. DIRECT  — Agent A proposes directly to Agent B. Both sign off. Execute.
 *   2. OPEN    — Agent A posts to open order book. Any eligible agent accepts.
 *   3. BATCH   — Multiple proposals settled in one tx. ~40% gas saving.
 *
 *  MEV protection:
 *   - Commit-reveal scheme: amountIn hidden until counterparty accepts
 *   - minAmountOut enforced on every swap via Uniswap v3
 *   - Flashbots Protect RPC recommended at the Python layer
 *
 *  Slashing:
 *   - Rejection: -1 rep for proposer
 *   - Failed execution: -5 rep, agent may be auto-suspended by registry
 */
contract AgentCoordinator is ReentrancyGuard, Ownable {
    using SafeERC20 for IERC20;

    // ── Types ──────────────────────────────────────────────────────────────

    enum ProposalStatus { Pending, Accepted, Executed, Rejected, Expired, Cancelled }
    enum ProposalType   { Direct, Open }

    struct TradeProposal {
        uint256        id;
        ProposalType   proposalType;
        address        proposer;
        address        counterparty;     // address(0) = open
        address        tokenIn;
        address        tokenOut;
        uint256        amountIn;
        uint256        minAmountOut;
        uint256        expiry;
        ProposalStatus status;
        bytes32        commitHash;       // keccak256(amountIn, nonce) for Direct
        bool           revealed;
    }

    struct BatchSettlement {
        uint256[] proposalIds;
        uint256   executedAt;
        uint256   successCount;
    }

    // ── Storage ────────────────────────────────────────────────────────────

    AgentRegistry public registry;
    ISwapRouter   public swapRouter;

    mapping(uint256 => TradeProposal)   public proposals;
    mapping(uint256 => BatchSettlement) public batches;
    mapping(address => uint256[])       public agentProposals;
    mapping(bytes32 => bool)            public usedCommits;

    uint256 public proposalCount;
    uint256 public batchCount;

    uint256 public constant PROPOSAL_TTL      = 300;
    uint256 public constant SLASH_FAILED_EXEC = 5;
    uint256 public constant MAX_BATCH_SIZE    = 20;

    address public treasury;

    // ── Events ─────────────────────────────────────────────────────────────

    event ProposalCreated(uint256 indexed id, ProposalType indexed proposalType, address indexed proposer, address counterparty, address tokenIn, address tokenOut);
    event ProposalAccepted(uint256 indexed id, address indexed acceptor);
    event ProposalRejected(uint256 indexed id, address indexed rejecter);
    event ProposalCancelled(uint256 indexed id);
    event AmountRevealed(uint256 indexed id, uint256 amountIn);
    event TradeExecuted(uint256 indexed id, address indexed proposer, address indexed counterparty, uint256 amountIn, uint256 amountOut);
    event BatchExecuted(uint256 indexed batchId, uint256 successCount);
    event AgentSlashed(address indexed agent, uint256 proposalId, int256 delta);

    // ── Modifiers ──────────────────────────────────────────────────────────

    modifier onlyActive() {
        require(registry.canParticipate(msg.sender), "Not an active agent with sufficient reputation");
        _;
    }

    modifier proposalExists(uint256 id) {
        require(id > 0 && id <= proposalCount, "Proposal does not exist");
        _;
    }

    modifier notExpired(uint256 id) {
        require(block.timestamp <= proposals[id].expiry, "Proposal expired");
        _;
    }

    // ── Constructor ────────────────────────────────────────────────────────

    constructor(address _registry, address _swapRouter, address _treasury) Ownable(msg.sender) {
        registry   = AgentRegistry(_registry);
        swapRouter = ISwapRouter(_swapRouter);
        treasury   = _treasury;
    }

    // ─────────────────────────────────────────────────────────────────────
    // PATH 1 — DIRECT (commit-reveal)
    // ─────────────────────────────────────────────────────────────────────

    /**
     * @notice Propose directly to a specific agent. amountIn is hidden until acceptance.
     * @param commitHash keccak256(abi.encodePacked(amountIn, nonce))
     */
    function proposeDirect(
        address counterparty,
        address tokenIn,
        address tokenOut,
        uint256 minAmountOut,
        bytes32 commitHash
    ) external onlyActive nonReentrant returns (uint256 proposalId) {
        require(counterparty != address(0) && counterparty != msg.sender, "Invalid counterparty");
        require(registry.canParticipate(counterparty), "Counterparty not eligible");
        require(!usedCommits[commitHash], "Commit already used");

        proposalId = _createProposal(ProposalType.Direct, msg.sender, counterparty, tokenIn, tokenOut, 0, minAmountOut, commitHash);
    }

    /**
     * @notice Reveal the committed amountIn after the proposal is accepted.
     */
    function revealAmount(uint256 proposalId, uint256 amountIn, bytes32 nonce)
        external proposalExists(proposalId)
    {
        TradeProposal storage p = proposals[proposalId];
        require(p.proposer == msg.sender,                "Not the proposer");
        require(p.status == ProposalStatus.Accepted,     "Must be accepted first");
        require(!p.revealed,                             "Already revealed");
        require(keccak256(abi.encodePacked(amountIn, nonce)) == p.commitHash, "Commit mismatch");

        p.amountIn = amountIn;
        p.revealed = true;
        usedCommits[p.commitHash] = true;
        emit AmountRevealed(proposalId, amountIn);
    }

    // ─────────────────────────────────────────────────────────────────────
    // PATH 2 — OPEN ORDER BOOK
    // ─────────────────────────────────────────────────────────────────────

    /**
     * @notice Post an open proposal. Any eligible agent can accept.
     */
    function proposeOpen(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minAmountOut
    ) external onlyActive nonReentrant returns (uint256 proposalId) {
        require(amountIn > 0, "amountIn must be > 0");
        proposalId = _createProposal(ProposalType.Open, msg.sender, address(0), tokenIn, tokenOut, amountIn, minAmountOut, bytes32(0));
    }

    function acceptProposal(uint256 proposalId)
        external onlyActive proposalExists(proposalId) notExpired(proposalId) nonReentrant
    {
        TradeProposal storage p = proposals[proposalId];
        require(p.status == ProposalStatus.Pending, "Not pending");
        require(p.proposer != msg.sender,            "Cannot accept own proposal");

        if (p.proposalType == ProposalType.Direct) {
            require(p.counterparty == msg.sender, "Not the intended counterparty");
        } else {
            p.counterparty = msg.sender;
        }

        p.status = ProposalStatus.Accepted;
        emit ProposalAccepted(proposalId, msg.sender);
        _tryNotifyAgent(p.counterparty, proposalId);
    }

    function rejectProposal(uint256 proposalId)
        external proposalExists(proposalId) nonReentrant
    {
        TradeProposal storage p = proposals[proposalId];
        require(p.status == ProposalStatus.Pending, "Not pending");
        require(p.counterparty == msg.sender || p.counterparty == address(0), "Not involved");

        p.status = ProposalStatus.Rejected;
        registry.updateReputation(p.proposer, -1);
        emit ProposalRejected(proposalId, msg.sender);
        emit AgentSlashed(p.proposer, proposalId, -1);
    }

    function cancelProposal(uint256 proposalId)
        external proposalExists(proposalId) nonReentrant
    {
        TradeProposal storage p = proposals[proposalId];
        require(p.proposer == msg.sender,           "Not the proposer");
        require(p.status == ProposalStatus.Pending, "Cannot cancel at this stage");
        p.status = ProposalStatus.Cancelled;
        emit ProposalCancelled(proposalId);
    }

    // ─────────────────────────────────────────────────────────────────────
    // SINGLE EXECUTION
    // ─────────────────────────────────────────────────────────────────────

    function executeMatch(uint256 proposalId)
        external proposalExists(proposalId) notExpired(proposalId) nonReentrant
    {
        TradeProposal storage p = proposals[proposalId];
        require(p.status == ProposalStatus.Accepted, "Not accepted");
        require(msg.sender == p.proposer || msg.sender == p.counterparty, "Not a party");
        if (p.proposalType == ProposalType.Direct) require(p.revealed, "Amount not revealed");

        p.status = ProposalStatus.Executed;
        uint256 amountOut = _settle(p);

        registry.updateReputation(p.proposer,     2);
        registry.updateReputation(p.counterparty, 2);
        registry.updateStats(p.proposer,     amountOut);
        registry.updateStats(p.counterparty, amountOut);
        _tryNotifyExecuted(p.proposer,     proposalId, amountOut);
        _tryNotifyExecuted(p.counterparty, proposalId, amountOut);

        emit TradeExecuted(proposalId, p.proposer, p.counterparty, p.amountIn, amountOut);
    }

    // ─────────────────────────────────────────────────────────────────────
    // PATH 3 — BATCH SETTLEMENT
    // ─────────────────────────────────────────────────────────────────────

    /**
     * @notice Execute up to MAX_BATCH_SIZE proposals in one transaction.
     *         Failed swaps are skipped and slashed — the batch does not revert.
     */
    function executeBatch(uint256[] calldata proposalIds)
        external nonReentrant returns (uint256 successCount)
    {
        require(proposalIds.length > 0 && proposalIds.length <= MAX_BATCH_SIZE, "Invalid batch size");

        uint256 batchId = ++batchCount;

        for (uint256 i = 0; i < proposalIds.length; i++) {
            uint256 pid = proposalIds[i];
            if (pid == 0 || pid > proposalCount) continue;

            TradeProposal storage p = proposals[pid];
            if (p.status != ProposalStatus.Accepted)               continue;
            if (block.timestamp > p.expiry)                        continue;
            if (p.proposalType == ProposalType.Direct && !p.revealed) continue;

            try this._executeOne(pid) {
                successCount++;
            } catch {
                _slashFailedExecution(p.proposer, pid);
            }
        }

        batches[batchId] = BatchSettlement({ proposalIds: proposalIds, executedAt: block.timestamp, successCount: successCount });
        emit BatchExecuted(batchId, successCount);
    }

    /// @dev Called via try/catch from executeBatch. Not a public API.
    function _executeOne(uint256 proposalId) external nonReentrant {
        require(msg.sender == address(this), "Internal only");
        TradeProposal storage p = proposals[proposalId];
        p.status = ProposalStatus.Executed;
        uint256 amountOut = _settle(p);
        registry.updateReputation(p.proposer,     2);
        registry.updateReputation(p.counterparty, 2);
        registry.updateStats(p.proposer,     amountOut);
        registry.updateStats(p.counterparty, amountOut);
        emit TradeExecuted(proposalId, p.proposer, p.counterparty, p.amountIn, amountOut);
    }

    // ─────────────────────────────────────────────────────────────────────
    // INTERNAL
    // ─────────────────────────────────────────────────────────────────────

    function _createProposal(
        ProposalType proposalType, address proposer, address counterparty,
        address tokenIn, address tokenOut, uint256 amountIn,
        uint256 minAmountOut, bytes32 commitHash
    ) internal returns (uint256 proposalId) {
        proposalId = ++proposalCount;
        proposals[proposalId] = TradeProposal({
            id:           proposalId,
            proposalType: proposalType,
            proposer:     proposer,
            counterparty: counterparty,
            tokenIn:      tokenIn,
            tokenOut:     tokenOut,
            amountIn:     amountIn,
            minAmountOut: minAmountOut,
            expiry:       block.timestamp + PROPOSAL_TTL,
            status:       ProposalStatus.Pending,
            commitHash:   commitHash,
            revealed:     proposalType == ProposalType.Open
        });
        agentProposals[proposer].push(proposalId);
        emit ProposalCreated(proposalId, proposalType, proposer, counterparty, tokenIn, tokenOut);
    }

    function _settle(TradeProposal storage p) internal returns (uint256 amountOut) {
        IERC20(p.tokenIn).safeTransferFrom(p.proposer, address(this), p.amountIn);
        IERC20(p.tokenIn).approve(address(swapRouter), p.amountIn);
        amountOut = swapRouter.exactInputSingle(ISwapRouter.ExactInputSingleParams({
            tokenIn:           p.tokenIn,
            tokenOut:          p.tokenOut,
            fee:               3000,
            recipient:         p.counterparty,
            deadline:          block.timestamp + 60,
            amountIn:          p.amountIn,
            amountOutMinimum:  p.minAmountOut,
            sqrtPriceLimitX96: 0
        }));
    }

    function _slashFailedExecution(address agent, uint256 proposalId) internal {
        registry.updateReputation(agent, -int256(SLASH_FAILED_EXEC));
        emit AgentSlashed(agent, proposalId, -int256(SLASH_FAILED_EXEC));
    }

    function _tryNotifyAgent(address agent, uint256 proposalId) internal {
        try IAgent(agent).onProposalReceived(proposalId) {} catch {}
    }

    function _tryNotifyExecuted(address agent, uint256 proposalId, uint256 amountOut) internal {
        try IAgent(agent).onTradeExecuted(proposalId, amountOut) {} catch {}
    }

    // ─────────────────────────────────────────────────────────────────────
    // VIEWS
    // ─────────────────────────────────────────────────────────────────────

    function getProposal(uint256 id) external view returns (TradeProposal memory) {
        return proposals[id];
    }

    function getAgentProposals(address agent) external view returns (uint256[] memory) {
        return agentProposals[agent];
    }

    function getOpenProposals(uint256 limit) external view returns (uint256[] memory open) {
        uint256 count = 0;
        uint256[] memory temp = new uint256[](limit);
        for (uint256 i = proposalCount; i >= 1 && count < limit; i--) {
            TradeProposal storage p = proposals[i];
            if (p.status == ProposalStatus.Pending && p.proposalType == ProposalType.Open && block.timestamp <= p.expiry) {
                temp[count++] = i;
            }
        }
        open = new uint256[](count);
        for (uint256 i = 0; i < count; i++) open[i] = temp[i];
    }
}
