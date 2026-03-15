# Security Audit Checklist

This document tracks the security review process before mainnet deployment.
Each item must be marked ✅ by the reviewer before the contract goes live.

---

## Smart Contract Audit

### AgentCoordinator

| Check | Status | Notes |
|---|---|---|
| Reentrancy guard on all state-changing functions | ✅ | `ReentrancyGuard` inherited from OpenZeppelin |
| SafeERC20 used for all token transfers | ✅ | `safeTransferFrom`, `safeTransfer` throughout |
| Integer overflow / underflow | ✅ | Solidity 0.8.x built-in checks |
| Access control on internal-only functions | ✅ | `executeMatchInternal` restricted to `address(this)` |
| Commit-reveal replay protection | ✅ | `usedCommits` mapping prevents reuse |
| Proposal expiry enforced | ✅ | `notExpired` modifier on accept + execute |
| Counterparty validation on Direct proposals | ✅ | `require(counterparty == msg.sender)` |
| Self-proposal blocked | ✅ | `require(proposer != msg.sender)` on accept |
| `minAmountOut` enforced on every swap | ✅ | Passed to Uniswap `amountOutMinimum` |
| Treasury address is non-zero | ⬜ | Verify in deploy script |
| No selfdestruct or delegatecall | ✅ | Not used anywhere |
| Events emitted for all state changes | ✅ | Verified in test suite |

### AgentRegistry

| Check | Status | Notes |
|---|---|---|
| `onlyOwner` on registerAgent / suspend / reactivate | ✅ | OpenZeppelin Ownable |
| `onlyCoordinator` on updateReputation / updateStats | ✅ | Set in `setCoordinator()` |
| Reputation clamped [0, MAX_REPUTATION] | ✅ | Both bounds enforced in `updateReputation()` |
| Duplicate registration prevented | ✅ | `isRegistered` check |
| Zero address rejected | ✅ | `require(wallet != address(0))` |

### General

| Check | Status | Notes |
|---|---|---|
| No tx.origin used for auth | ✅ | All auth via `msg.sender` |
| No block.timestamp for critical logic | ✅ | Only used for proposal TTL (acceptable) |
| No hardcoded addresses except known protocols | ✅ | Uniswap router injected at construction |
| Contract size within EIP-170 limit (24KB) | ⬜ | Run `npx hardhat size-contracts` |
| Gas limits on batch operations | ✅ | `MAX_BATCH_SIZE = 20` |
| Fallback / receive functions absent | ✅ | Not needed — no ETH handling |

---

## MEV & Front-Running

| Check | Status | Notes |
|---|---|---|
| Direct proposals use commit-reveal | ✅ | `amountIn` hidden until acceptance |
| All swaps have `minAmountOut` | ✅ | Checked at contract level |
| Flashbots Protect RPC enabled | ⬜ | Enforced in Python `A2AClient` config |
| Agent tick intervals include jitter | ⬜ | Add ±10s random delay in `BaseAgent.start()` |

---

## Key Management

| Check | Status | Notes |
|---|---|---|
| `.env` in `.gitignore` | ✅ | Confirmed |
| No private keys in source code or comments | ✅ | Confirmed |
| Deployer is not the same as agent wallets | ⬜ | Use separate keys in production |
| Treasury is a multisig (not an EOA) | ⬜ | Replace `treasury = deployer.address` in deploy.js |
| Agent wallets funded with only operational ETH | ⬜ | Do not store large balances in agent wallets |

---

## External Dependencies

| Dependency | Version | Risk | Mitigation |
|---|---|---|---|
| OpenZeppelin Contracts | 5.x | Low | Widely audited |
| Uniswap v3 Router | Deployed | Low | Official deployment, immutable |
| The Graph | Hosted | Medium | Add fallback to direct RPC calls |
| Binance WS | External | Medium | Reconnection logic implemented |
| Anthropic API | External | Low | Neutral default on failure |
| Flashbots Protect | External | Low | Fallback to public mempool |

---

## Before Mainnet — Mandatory

- [ ] Engage external auditor (Certik / Trail of Bits / Code4rena)
- [ ] Resolve all Critical and High findings
- [ ] Deploy and run full lifecycle on Arbitrum Sepolia for ≥7 days
- [ ] Set treasury to Gnosis Safe multisig (3-of-5 signers)
- [ ] Cap initial capital per agent at $500 for first 30 days
- [ ] Set up monitoring webhook (Slack / PagerDuty)
- [ ] Document emergency pause procedure

---

## Audit History

| Date | Auditor | Scope | Report |
|---|---|---|---|
| — | Not yet scheduled | — | — |

---

*Last updated: 2025*  
*Maintainer: Agenopoly team*
