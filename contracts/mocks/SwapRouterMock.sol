// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../interfaces/ISwapRouter.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @dev Mock Uniswap v3 router — returns minAmountOut directly without a real swap.
contract SwapRouterMock is ISwapRouter {
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable override returns (uint256 amountOut)
    {
        // Pull tokenIn from caller
        IERC20(params.tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
        // Return exactly minAmountOut to satisfy slippage check
        amountOut = params.amountOutMinimum == 0 ? params.amountIn : params.amountOutMinimum;
        // In real tests, pre-mint tokenOut to this contract before calling
    }
}
