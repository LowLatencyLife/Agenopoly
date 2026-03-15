const { ethers } = require("hardhat");
const fs = require("fs");

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log(`\n${"=".repeat(55)}`);
  console.log(`  Agenopoly — Phase 3 Deployment`);
  console.log(`${"=".repeat(55)}`);
  console.log(`  Deployer : ${deployer.address}`);
  console.log(`  Balance  : ${ethers.formatEther(await deployer.provider.getBalance(deployer.address))} ETH`);
  console.log(`  Network  : ${(await ethers.provider.getNetwork()).name}`);
  console.log(`${"=".repeat(55)}\n`);

  const treasury = deployer.address; // Replace with multisig in production

  // 1. AgentRegistry
  console.log("1/3  Deploying AgentRegistry...");
  const AgentRegistry = await ethers.getContractFactory("AgentRegistry");
  const registry = await AgentRegistry.deploy();
  await registry.waitForDeployment();
  const registryAddr = await registry.getAddress();
  console.log(`     ✓ AgentRegistry: ${registryAddr}`);

  // 2. AgentCoordinator
  const UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"; // Arbitrum
  console.log("2/3  Deploying AgentCoordinator...");
  const AgentCoordinator = await ethers.getContractFactory("AgentCoordinator");
  const coordinator = await AgentCoordinator.deploy(registryAddr, UNISWAP_V3_ROUTER, treasury);
  await coordinator.waitForDeployment();
  const coordinatorAddr = await coordinator.getAddress();
  console.log(`     ✓ AgentCoordinator: ${coordinatorAddr}`);

  // 3. Wire up: grant coordinator permission to update registry
  console.log("3/3  Wiring contracts...");
  const tx = await registry.setCoordinator(coordinatorAddr);
  await tx.wait();
  console.log(`     ✓ Registry.coordinator = AgentCoordinator`);

  // Save deployment manifest
  const manifest = {
    network:    (await ethers.provider.getNetwork()).name,
    chainId:    Number((await ethers.provider.getNetwork()).chainId),
    deployedAt: new Date().toISOString(),
    deployer:   deployer.address,
    treasury,
    contracts: {
      AgentRegistry:    registryAddr,
      AgentCoordinator: coordinatorAddr,
    },
    externalContracts: {
      UniswapV3Router: UNISWAP_V3_ROUTER,
    },
  };

  fs.writeFileSync("deployments.json", JSON.stringify(manifest, null, 2));
  console.log(`\n  Manifest saved to deployments.json`);
  console.log(`\nAdd to your .env:\n`);
  console.log(`COORDINATOR_ADDRESS=${coordinatorAddr}`);
  console.log(`REGISTRY_ADDRESS=${registryAddr}`);
}

main().catch((err) => { console.error(err); process.exitCode = 1; });
