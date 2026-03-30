import * as anchor from "@coral-xyz/anchor";
import { Program } from "@coral-xyz/anchor";
import { NodeReputation } from "../target/types/node_reputation";
import { assert } from "chai";

describe("node-reputation", () => {
  const provider = anchor.AnchorProvider.env();
  anchor.setProvider(provider);

  const program = anchor.workspace.NodeReputation as Program<NodeReputation>;
  const operator = provider.wallet;

  let networkStatsPda: anchor.web3.PublicKey;
  let nodePda: anchor.web3.PublicKey;
  let heartbeatPda: anchor.web3.PublicKey;

  before(async () => {
    [networkStatsPda] = anchor.web3.PublicKey.findProgramAddressSync(
      [Buffer.from("network-stats")],
      program.programId
    );
    [nodePda] = anchor.web3.PublicKey.findProgramAddressSync(
      [Buffer.from("node"), operator.publicKey.toBuffer()],
      program.programId
    );
    [heartbeatPda] = anchor.web3.PublicKey.findProgramAddressSync(
      [Buffer.from("heartbeat"), operator.publicKey.toBuffer()],
      program.programId
    );
  });

  it("initializes network stats", async () => {
    await program.methods
      .initialize()
      .accounts({
        authority: operator.publicKey,
        networkStats: networkStatsPda,
        systemProgram: anchor.web3.SystemProgram.programId,
      })
      .rpc();

    const stats = await program.account.networkStats.fetch(networkStatsPda);
    assert.equal(stats.totalNodes, 0);
    assert.equal(stats.activeNodes, 0);
    assert.ok(stats.authority.equals(operator.publicKey));
  });

  it("registers a node", async () => {
    const solaxyWallet = anchor.web3.Keypair.generate().publicKey;
    const celestiaAddress = Buffer.alloc(32, 0xab);

    await program.methods
      .registerNode(solaxyWallet, Array.from(celestiaAddress), { both: {} }, "https://example.com/node-meta")
      .accounts({
        operator: operator.publicKey,
        node: nodePda,
        heartbeatBuffer: heartbeatPda,
        networkStats: networkStatsPda,
        systemProgram: anchor.web3.SystemProgram.programId,
      })
      .rpc();

    const node = await program.account.nodeRegistry.fetch(nodePda);
    assert.ok(node.authority.equals(operator.publicKey));
    assert.ok(node.solaxyWallet.equals(solaxyWallet));
    assert.equal(node.isActive, true);
    assert.equal(node.reputationScore, 0);
    assert.equal(node.totalHeartbeats.toNumber(), 0);
    assert.equal(node.metadataUri, "https://example.com/node-meta");

    const stats = await program.account.networkStats.fetch(networkStatsPda);
    assert.equal(stats.totalNodes, 1);
    assert.equal(stats.activeNodes, 1);
    assert.equal(stats.totalSequencers, 1);
    assert.equal(stats.totalProvers, 1);
  });

  it("submits a heartbeat", async () => {
    await program.methods
      .submitHeartbeat(
        new anchor.BN(1000),  // solaxy_block_height
        new anchor.BN(500),   // celestia_das_height
        0x0f,                 // services_healthy (all 4 up)
        9500,                 // uptime_pct (95.00%)
        35,                   // cpu_usage
        12,                   // peer_count
        new anchor.BN(990),   // attested_height
      )
      .accounts({
        operator: operator.publicKey,
        node: nodePda,
        heartbeatBuffer: heartbeatPda,
        networkStats: networkStatsPda,
        authority: operator.publicKey,
      })
      .rpc();

    const node = await program.account.nodeRegistry.fetch(nodePda);
    assert.equal(node.totalHeartbeats.toNumber(), 1);
    assert.ok(node.lastHeartbeat.toNumber() > 0);
    assert.ok(node.reputationScore > 0);

    const stats = await program.account.networkStats.fetch(networkStatsPda);
    assert.equal(stats.maxBlockHeight.toNumber(), 1000);
  });

  it("rejects heartbeat too soon", async () => {
    try {
      await program.methods
        .submitHeartbeat(
          new anchor.BN(1001), new anchor.BN(501), 0x0f, 9500, 35, 12, new anchor.BN(991),
        )
        .accounts({
          operator: operator.publicKey,
          node: nodePda,
          heartbeatBuffer: heartbeatPda,
          networkStats: networkStatsPda,
          authority: operator.publicKey,
        })
        .rpc();
      assert.fail("Should have rejected heartbeat");
    } catch (err) {
      assert.include(err.toString(), "HeartbeatTooFrequent");
    }
  });

  it("updates metadata", async () => {
    await program.methods
      .updateMetadata("https://example.com/node-v2")
      .accounts({
        operator: operator.publicKey,
        node: nodePda,
      })
      .rpc();

    const node = await program.account.nodeRegistry.fetch(nodePda);
    assert.equal(node.metadataUri, "https://example.com/node-v2");
  });

  it("deactivates a node", async () => {
    await program.methods
      .deactivateNode()
      .accounts({
        operator: operator.publicKey,
        node: nodePda,
        networkStats: networkStatsPda,
      })
      .rpc();

    const node = await program.account.nodeRegistry.fetch(nodePda);
    assert.equal(node.isActive, false);

    const stats = await program.account.networkStats.fetch(networkStatsPda);
    assert.equal(stats.activeNodes, 0);
  });

  it("rejects heartbeat for inactive node", async () => {
    try {
      await program.methods
        .submitHeartbeat(
          new anchor.BN(1002), new anchor.BN(502), 0x0f, 9500, 35, 12, new anchor.BN(992),
        )
        .accounts({
          operator: operator.publicKey,
          node: nodePda,
          heartbeatBuffer: heartbeatPda,
          networkStats: networkStatsPda,
          authority: operator.publicKey,
        })
        .rpc();
      assert.fail("Should have rejected heartbeat for inactive node");
    } catch (err) {
      assert.include(err.toString(), "NodeNotActive");
    }
  });
});
