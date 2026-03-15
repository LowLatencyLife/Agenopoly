/**
 * register_agents.js — Register deployed agents in the AgentRegistry.
 *
 * Usage:
 *   npx hardhat run scripts/register_agents.js --network arbitrum
 *
 * Reads agent addresses from .env and registers them with their strategy IDs.
 */

const { ethers } = require("hardhat");
const fs         = require("fs");
require("dotenv").config();

const STRATEGIES = {
  TREND_FOLLOWER: ethers.keccak256(ethers.toUtf8Bytes("TREND_FOLLOWER")),
  ARBITRAGE:      ethers.keccak256(ethers.toUtf8Bytes("ARBITRAGE")),
  MARKET_MAKER:   ethers.keccak256(ethers.toUtf8Bytes("MARKET_MAKER")),
};

// Add your agents here
const AGENTS_TO_REGISTER = [
  { envKey: "AGENT_A_PRIVATE_KEY", strategy: "TREND_FOLLOWER", name: "Alpha" },
  { envKey: "AGENT_B_PRIVATE_KEY", strategy: "ARBITRAGE",      name: "Beta"  },
];

async function main() {
  const deps = JSON.parse(fs.readFileSync("deployments.json", "utf8"));
  const registryAddr = deps.contracts.AgentRegistry;

  const [owner] = await ethers.getSigners();
  const registry = await ethers.getContractAt("AgentRegistry", registryAddr);

  console.log(`\nRegistering agents on ${deps.network}`);
  console.log(`Registry: ${registryAddr}\n`);

  for (const agent of AGENTS_TO_REGISTER) {
    const privKey = process.env[agent.envKey];
    if (!privKey) {
      console.warn(`  ⚠️  ${agent.envKey} not set — skipping ${agent.name}`);
      continue;
    }

    const wallet  = new ethers.Wallet(privKey);
    const address = wallet.address;
    const alreadyRegistered = await registry.isRegistered(address);

    if (alreadyRegistered) {
      console.log(`  ✅ ${agent.name} (${address.slice(0,10)}...) already registered`);
      continue;
    }

    const tx = await registry.registerAgent(address, STRATEGIES[agent.strategy], agent.name);
    await tx.wait();
    console.log(`  ✅ Registered ${agent.name} | ${address} | strategy: ${agent.strategy}`);
  }

  console.log("\nDone. Run status check:");
  console.log("  python scripts/migrate.py --network arbitrum --dry-run\n");
}

main().catch(err => { console.error(err); process.exitCode = 1; });
