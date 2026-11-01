// rotate-secret.mjs
//
// Generates a new entity secret, registers it with Circle, tests it, and updates .env.
//
// Usage: node --env-file=../.env rotate-secret.mjs

import crypto from "crypto";
import fs from "fs";
import path from "path";
import { registerEntitySecretCiphertext } from "@circle-fin/developer-controlled-wallets";

const API_KEY = process.env.CIRCLE_API_KEY;
const WALLET_ID = process.env.WALLET_ID;
const API = "https://api.circle.com/v1/w3s";

// Step 1: Generate fresh 32-byte secret
const newSecret = crypto.randomBytes(32).toString("hex");
console.log("=== Rotating Entity Secret ===");
console.log(`New secret: ${newSecret}\n`);

// Step 2: Register with Circle
console.log("Registering with Circle...");
try {
  const response = await registerEntitySecretCiphertext({
    apiKey: API_KEY,
    entitySecret: newSecret,
  });

  if (response.data?.recoveryFile) {
    const recoveryPath = path.join(import.meta.dirname, "recovery_file.dat");
    fs.writeFileSync(recoveryPath, response.data.recoveryFile);
    console.log("📦 Recovery file saved to wallet-setup/recovery_file.dat");
  }
  console.log("✅ Registered!\n");
} catch (e) {
  // SDK 10.x throws its own HttpResponseError (status on the error, axios error
  // on .error); 7.x threw the axios error itself (status on .response). Read both.
  if ((e.status ?? e.response?.status) === 409) {
    console.error("❌ 409 Conflict — an entity secret is already registered.");
    console.error("   Go to https://console.circle.com → Programmable Wallets → Configuration");
    console.error("   Click 'Reset Entity Secret', then re-run this script.\n");
  } else {
    console.error("❌ Registration failed:", e.message?.slice(0, 300), "\n");
  }
  console.log("Generated secret (save somewhere safe):");
  console.log(newSecret);
  process.exit(1);
}

// Step 3: Test the new secret with a contract call
console.log("Testing new secret...");
const pkRes = await fetch(`${API}/config/entity/publicKey`, {
  headers: { Authorization: `Bearer ${API_KEY}`, Accept: "application/json" },
});
const pkData = await pkRes.json();
const pub = crypto.createPublicKey({ key: pkData.data.publicKey, format: "pem" });
const ct = crypto.publicEncrypt(
  { key: pub, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" },
  Buffer.from(newSecret, "hex")
).toString("base64");

const testRes = await fetch(`${API}/developer/transactions/contractExecution`, {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}`, "Content-Type": "application/json", Accept: "application/json" },
  body: JSON.stringify({
    idempotencyKey: crypto.randomUUID(),
    walletId: WALLET_ID,
    contractAddress: "0x8c77f2920a7d440dc07d824fbe7e39166c5a27a0",
    abiFunctionSignature: "setPrice(uint256)",
    abiParameters: ["433450000"],
    feeLevel: "MEDIUM",
    entitySecretCiphertext: ct,
  }),
});
const testData = await testRes.json();

if (testRes.status >= 400) {
  console.error(`❌ Test failed (${testRes.status}): ${testData.message}`);
  console.log("\nNew secret (may need Console reset first):");
  console.log(newSecret);
  process.exit(1);
}

console.log(`✅ Test TX accepted: ${testData.data?.id}\n`);

// Step 4: Update .env
const envPath = path.resolve(import.meta.dirname, "../.env");
let envContent = fs.readFileSync(envPath, "utf8");
envContent = envContent.replace(
  /CIRCLE_ENTITY_SECRET=.*/,
  `CIRCLE_ENTITY_SECRET=${newSecret}`
);
fs.writeFileSync(envPath, envContent);
console.log("✅ .env updated with new entity secret");
console.log("\n=== Rotation Complete ===");
