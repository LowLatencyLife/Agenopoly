// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title  ISwapRouter
 * @notice Minimal Uniswap v3 SwapRouter interface used by AgentCoordinator.
 *         Full interface: https://github.com/Uniswap/v3-periphery
 */
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    /**
     * @notice Swap an exact amount of tokenIn for as much tokenOut as possible.
     * @return amountOut The amount of tokenOut received.
     */
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable returns (uint256 amountOut);
}
