// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  IAgent
 * @notice Interface that every on-chain Agenopoly agent must implement.
 */
interface IAgent {
    /// @notice Called by the coordinator when a proposal targeting this agent is created.
    function onProposalReceived(uint256 proposalId) external returns (bool accept);

    /// @notice Called after a trade is successfully executed.
    function onTradeExecuted(uint256 proposalId, uint256 amountOut) external;

    /// @notice Returns the agent's current strategy identifier.
    function strategyId() external view returns (bytes32);

    /// @notice Returns the agent's on-chain reputation score.
    function reputationScore() external view returns (int256);
}
