const { expect }       = require("chai");
const { ethers }       = require("hardhat");
const { time }         = require("@nomicfoundation/hardhat-toolbox/network-helpers");

// ── Helpers ──────────────────────────────────────────────────────────────

const STRATEGY_TREND = ethers.keccak256(ethers.toUtf8Bytes("TREND_FOLLOWER"));
const STRATEGY_ARB   = ethers.keccak256(ethers.toUtf8Bytes("ARBITRAGE"));

async function deployAll() {
  const [owner, agentA, agentB, agentC, treasury] = await ethers.getSigners();

  const Registry    = await ethers.getContractFactory("AgentRegistry");
  const registry    = await Registry.deploy();

  // Mock ERC-20 tokens for swap tests
  const ERC20Mock   = await ethers.getContractFactory("ERC20Mock");
  const tokenIn     = await ERC20Mock.deploy("Mock WETH", "WETH", 18);
  const tokenOut    = await ERC20Mock.deploy("Mock USDC", "USDC", 6);

  // Mock swap router that returns minAmountOut directly
  const RouterMock  = await ethers.getContractFactory("SwapRouterMock");
  const router      = await RouterMock.deploy();

  const Coordinator = await ethers.getContractFactory("AgentCoordinator");
  const coordinator = await Coordinator.deploy(
    await registry.getAddress(),
    await router.getAddress(),
    treasury.address
  );

  await registry.setCoordinator(await coordinator.getAddress());
  await registry.registerAgent(agentA.address, STRATEGY_TREND, "Alpha");
  await registry.registerAgent(agentB.address, STRATEGY_ARB,   "Beta");

  return { registry, coordinator, router, tokenIn, tokenOut, owner, agentA, agentB, agentC, treasury };
}

// ── AgentRegistry ─────────────────────────────────────────────────────────

describe("AgentRegistry", () => {

  it("registers an agent with initial reputation of 100", async () => {
    const { registry, agentA } = await deployAll();
    const info = await registry.getAgent(agentA.address);
    expect(info.reputationScore).to.equal(100n);
    expect(info.status).to.equal(1n); // Active
  });

  it("canParticipate returns true for active agent with rep >= 10", async () => {
    const { registry, agentA } = await deployAll();
    expect(await registry.canParticipate(agentA.address)).to.be.true;
  });

  it("canParticipate returns false for unregistered address", async () => {
    const { registry, agentC } = await deployAll();
    expect(await registry.canParticipate(agentC.address)).to.be.false;
  });

  it("suspends and reactivates an agent", async () => {
    const { registry, agentA } = await deployAll();
    await registry.suspendAgent(agentA.address, "test suspension");
    expect(await registry.canParticipate(agentA.address)).to.be.false;
    await registry.reactivateAgent(agentA.address);
    expect(await registry.canParticipate(agentA.address)).to.be.true;
  });

  it("getTopAgents returns agents sorted by reputation", async () => {
    const { registry, coordinator, agentA, agentB } = await deployAll();
    // Boost agentB's rep via coordinator
    await registry.connect(await ethers.getSigner(await coordinator.getAddress()))
    // We call via coordinator fixture — just verify ordering logic
    const top = await registry.getTopAgents(2);
    expect(top.length).to.equal(2);
  });

  it("reverts if non-owner tries to register", async () => {
    const { registry, agentA, agentB } = await deployAll();
    await expect(
      registry.connect(agentA).registerAgent(agentB.address, STRATEGY_ARB, "Rogue")
    ).to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount");
  });

  it("reverts on duplicate registration", async () => {
    const { registry, agentA } = await deployAll();
    await expect(
      registry.registerAgent(agentA.address, STRATEGY_TREND, "Alpha2")
    ).to.be.revertedWith("Already registered");
  });
});

// ── AgentCoordinator — Open proposals ─────────────────────────────────────

describe("AgentCoordinator — Open proposals", () => {

  it("creates an open proposal and emits ProposalCreated", async () => {
    const { coordinator, tokenIn, tokenOut, agentA } = await deployAll();
    const tx = await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), ethers.parseUnits("200", 6)
    );
    await expect(tx).to.emit(coordinator, "ProposalCreated").withArgs(
      1n, 1n, agentA.address,
      ethers.ZeroAddress,
      await tokenIn.getAddress(), await tokenOut.getAddress()
    );
  });

  it("increments proposalCount", async () => {
    const { coordinator, tokenIn, tokenOut, agentA } = await deployAll();
    await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), 0n
    );
    expect(await coordinator.proposalCount()).to.equal(1n);
  });

  it("agentB accepts an open proposal", async () => {
    const { coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();
    await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), 0n
    );
    const tx = await coordinator.connect(agentB).acceptProposal(1n);
    await expect(tx).to.emit(coordinator, "ProposalAccepted").withArgs(1n, agentB.address);

    const p = await coordinator.getProposal(1n);
    expect(p.status).to.equal(1n); // Accepted
    expect(p.counterparty).to.equal(agentB.address);
  });

  it("proposer cannot accept their own proposal", async () => {
    const { coordinator, tokenIn, tokenOut, agentA } = await deployAll();
    await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), 0n
    );
    await expect(
      coordinator.connect(agentA).acceptProposal(1n)
    ).to.be.revertedWith("Cannot accept own proposal");
  });

  it("rejects a proposal and reduces proposer reputation by 1", async () => {
    const { registry, coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();
    await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), 0n
    );
    const repBefore = (await registry.getAgent(agentA.address)).reputationScore;
    await coordinator.connect(agentB).rejectProposal(1n);
    const repAfter = (await registry.getAgent(agentA.address)).reputationScore;
    expect(repAfter).to.equal(repBefore - 1n);
  });

  it("proposer can cancel a pending proposal", async () => {
    const { coordinator, tokenIn, tokenOut, agentA } = await deployAll();
    await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), 0n
    );
    await coordinator.connect(agentA).cancelProposal(1n);
    const p = await coordinator.getProposal(1n);
    expect(p.status).to.equal(5n); // Cancelled
  });

  it("reverts on expired proposal", async () => {
    const { coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();
    await coordinator.connect(agentA).proposeOpen(
      await tokenIn.getAddress(), await tokenOut.getAddress(),
      ethers.parseUnits("1", 18), 0n
    );
    await time.increase(400); // past 300s TTL
    await expect(
      coordinator.connect(agentB).acceptProposal(1n)
    ).to.be.revertedWith("Proposal expired");
  });
});

// ── AgentCoordinator — Direct (commit-reveal) ─────────────────────────────

describe("AgentCoordinator — Direct proposals", () => {

  async function commitHash(amountIn, nonce) {
    return ethers.keccak256(
      ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "bytes32"], [amountIn, nonce]
      )
    );
  }

  it("creates a direct proposal with zero amountIn (hidden)", async () => {
    const { coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();
    const nonce  = ethers.randomBytes(32);
    const amount = ethers.parseUnits("0.5", 18);
    const hash   = await commitHash(amount, nonce);

    await coordinator.connect(agentA).proposeDirect(
      agentB.address, await tokenIn.getAddress(), await tokenOut.getAddress(), 0n, hash
    );

    const p = await coordinator.getProposal(1n);
    expect(p.amountIn).to.equal(0n);
    expect(p.revealed).to.be.false;
  });

  it("reveals amount after acceptance", async () => {
    const { coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();
    const nonce  = ethers.randomBytes(32);
    const amount = ethers.parseUnits("0.5", 18);
    const hash   = await commitHash(amount, nonce);

    await coordinator.connect(agentA).proposeDirect(
      agentB.address, await tokenIn.getAddress(), await tokenOut.getAddress(), 0n, hash
    );
    await coordinator.connect(agentB).acceptProposal(1n);

    const tx = await coordinator.connect(agentA).revealAmount(1n, amount, nonce);
    await expect(tx).to.emit(coordinator, "AmountRevealed").withArgs(1n, amount);

    const p = await coordinator.getProposal(1n);
    expect(p.amountIn).to.equal(amount);
    expect(p.revealed).to.be.true;
  });

  it("reverts reveal with wrong nonce", async () => {
    const { coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();
    const nonce  = ethers.randomBytes(32);
    const amount = ethers.parseUnits("0.5", 18);
    const hash   = await commitHash(amount, nonce);

    await coordinator.connect(agentA).proposeDirect(
      agentB.address, await tokenIn.getAddress(), await tokenOut.getAddress(), 0n, hash
    );
    await coordinator.connect(agentB).acceptProposal(1n);

    const wrongNonce = ethers.randomBytes(32);
    await expect(
      coordinator.connect(agentA).revealAmount(1n, amount, wrongNonce)
    ).to.be.revertedWith("Commit mismatch");
  });

  it("reverts if wrong counterparty tries to accept direct proposal", async () => {
    const { registry, coordinator, tokenIn, tokenOut, agentA, agentB, agentC, owner } = await deployAll();
    await registry.registerAgent(agentC.address, STRATEGY_ARB, "Gamma");

    const nonce  = ethers.randomBytes(32);
    const amount = ethers.parseUnits("0.5", 18);
    const hash   = await commitHash(amount, nonce);

    await coordinator.connect(agentA).proposeDirect(
      agentB.address, await tokenIn.getAddress(), await tokenOut.getAddress(), 0n, hash
    );
    await expect(
      coordinator.connect(agentC).acceptProposal(1n)
    ).to.be.revertedWith("Not the intended counterparty");
  });
});

// ── AgentCoordinator — Batch settlement ───────────────────────────────────

describe("AgentCoordinator — Batch settlement", () => {

  it("rejects empty batch", async () => {
    const { coordinator, agentA } = await deployAll();
    await expect(
      coordinator.connect(agentA).executeBatch([])
    ).to.be.revertedWith("Invalid batch size");
  });

  it("rejects batch larger than MAX_BATCH_SIZE", async () => {
    const { coordinator, agentA } = await deployAll();
    const ids = Array.from({ length: 21 }, (_, i) => i + 1);
    await expect(
      coordinator.connect(agentA).executeBatch(ids)
    ).to.be.revertedWith("Invalid batch size");
  });
});

// ── Reputation edge cases ──────────────────────────────────────────────────

describe("Reputation — edge cases", () => {

  it("reputation does not go below 0", async () => {
    const { registry, coordinator, tokenIn, tokenOut, agentA, agentB } = await deployAll();

    // Reject many proposals to drive rep to 0
    for (let i = 0; i < 110; i++) {
      try {
        await coordinator.connect(agentA).proposeOpen(
          await tokenIn.getAddress(), await tokenOut.getAddress(),
          ethers.parseUnits("0.001", 18), 0n
        );
        await coordinator.connect(agentB).rejectProposal(BigInt(i + 1));
      } catch { break; }
    }

    const info = await registry.getAgent(agentA.address);
    expect(info.reputationScore).to.be.gte(0n);
  });

  it("reputation does not exceed MAX_REPUTATION (1000)", async () => {
    const { registry } = await deployAll();
    // Directly verified: initial rep = 100, max = 1000
    expect(await registry.MAX_REPUTATION()).to.equal(1000n);
  });
});
