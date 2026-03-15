// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title  AgentRegistry
 * @notice Single source of truth for all registered Agenopoly agents.
 *         Stores metadata, reputation scores, and trading statistics.
 *
 * @dev    AgentCoordinator reads reputation from here before allowing proposals.
 *         Only the Registry owner (deployer/DAO) can register new agents.
 */
contract AgentRegistry is Ownable {

    // ── Types ──────────────────────────────────────────────────────────────

    enum AgentStatus { Inactive, Active, Suspended }

    struct AgentInfo {
        address wallet;
        bytes32 strategyId;       // e.g. keccak256("TREND_FOLLOWER")
        string  name;
        int256  reputationScore;
        uint256 totalTrades;
        uint256 totalVolumeUsd;   // scaled by 1e6 (USDC decimals)
        uint256 registeredAt;
        AgentStatus status;
    }

    // ── Storage ────────────────────────────────────────────────────────────

    mapping(address => AgentInfo) public agents;
    address[]                     public agentList;
    mapping(address => bool)      public isRegistered;

    address public coordinator;   // AgentCoordinator — only one allowed to update stats

    int256  public constant INITIAL_REPUTATION  = 100;
    int256  public constant MIN_REPUTATION       = 10;
    int256  public constant MAX_REPUTATION       = 1000;

    // ── Events ─────────────────────────────────────────────────────────────

    event AgentRegistered(address indexed wallet, bytes32 strategyId, string name);
    event AgentSuspended(address indexed wallet, string reason);
    event AgentReactivated(address indexed wallet);
    event ReputationUpdated(address indexed wallet, int256 delta, int256 newScore);
    event StatsUpdated(address indexed wallet, uint256 totalTrades, uint256 totalVolumeUsd);
    event CoordinatorSet(address indexed coordinator);

    // ── Modifiers ──────────────────────────────────────────────────────────

    modifier onlyCoordinator() {
        require(msg.sender == coordinator, "AgentRegistry: caller is not coordinator");
        _;
    }

    modifier agentExists(address wallet) {
        require(isRegistered[wallet], "AgentRegistry: agent not registered");
        _;
    }

    // ── Constructor ────────────────────────────────────────────────────────

    constructor() Ownable(msg.sender) {}

    // ── Admin ──────────────────────────────────────────────────────────────

    function setCoordinator(address _coordinator) external onlyOwner {
        require(_coordinator != address(0), "Zero address");
        coordinator = _coordinator;
        emit CoordinatorSet(_coordinator);
    }

    // ── Registration ───────────────────────────────────────────────────────

    function registerAgent(
        address wallet,
        bytes32 strategyId,
        string calldata name
    ) external onlyOwner {
        require(!isRegistered[wallet], "Already registered");
        require(wallet != address(0),  "Zero address");

        agents[wallet] = AgentInfo({
            wallet:           wallet,
            strategyId:       strategyId,
            name:             name,
            reputationScore:  INITIAL_REPUTATION,
            totalTrades:      0,
            totalVolumeUsd:   0,
            registeredAt:     block.timestamp,
            status:           AgentStatus.Active
        });

        isRegistered[wallet] = true;
        agentList.push(wallet);

        emit AgentRegistered(wallet, strategyId, name);
    }

    function suspendAgent(address wallet, string calldata reason)
        external onlyOwner agentExists(wallet)
    {
        agents[wallet].status = AgentStatus.Suspended;
        emit AgentSuspended(wallet, reason);
    }

    function reactivateAgent(address wallet)
        external onlyOwner agentExists(wallet)
    {
        require(agents[wallet].status == AgentStatus.Suspended, "Not suspended");
        agents[wallet].status = AgentStatus.Active;
        emit AgentReactivated(wallet);
    }

    // ── Called by AgentCoordinator ─────────────────────────────────────────

    function updateReputation(address wallet, int256 delta)
        external onlyCoordinator agentExists(wallet)
    {
        int256 current = agents[wallet].reputationScore;
        int256 updated = current + delta;
        if (updated < 0)                updated = 0;
        if (updated > MAX_REPUTATION)   updated = MAX_REPUTATION;
        agents[wallet].reputationScore = updated;
        emit ReputationUpdated(wallet, delta, updated);
    }

    function updateStats(address wallet, uint256 volumeUsd)
        external onlyCoordinator agentExists(wallet)
    {
        agents[wallet].totalTrades++;
        agents[wallet].totalVolumeUsd += volumeUsd;
        emit StatsUpdated(wallet, agents[wallet].totalTrades, agents[wallet].totalVolumeUsd);
    }

    // ── Views ──────────────────────────────────────────────────────────────

    function getAgent(address wallet) external view returns (AgentInfo memory) {
        return agents[wallet];
    }

    function canParticipate(address wallet) external view returns (bool) {
        if (!isRegistered[wallet]) return false;
        AgentInfo memory a = agents[wallet];
        return a.status == AgentStatus.Active && a.reputationScore >= MIN_REPUTATION;
    }

    function getAllAgents() external view returns (address[] memory) {
        return agentList;
    }

    function getTopAgents(uint256 n) external view returns (address[] memory top) {
        uint256 len = agentList.length;
        if (n > len) n = len;
        top = new address[](n);
        // Simple selection sort — acceptable for small agent sets
        address[] memory copy = agentList;
        for (uint256 i = 0; i < n; i++) {
            uint256 best = i;
            for (uint256 j = i + 1; j < len; j++) {
                if (agents[copy[j]].reputationScore > agents[copy[best]].reputationScore) {
                    best = j;
                }
            }
            (copy[i], copy[best]) = (copy[best], copy[i]);
            top[i] = copy[i];
        }
    }
}
