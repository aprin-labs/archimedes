// register-entity-secret.mjs
//
// Generates a 32-byte entity secret and registers it with Circle.
// Idempotent: skips if CIRCLE_ENTITY_SECRET already set and active.
//
// Usage: node --env-file=../.env register-entity-secret.mjs

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

import { registerEntitySecretCiphertext } from "@circle-fin/developer-controlled-wallets";

const API_KEY = process.env.CIRCLE_API_KEY;
const EXISTING_SECRET = process.env.CIRCLE_ENTITY_SECRET;

if (!API_KEY) {
  console.error("ERROR: CIRCLE_API_KEY not found in ../.env");
  process.exit(1);
}

if (EXISTING_SECRET) {
  console.log("CIRCLE_ENTITY_SECRET already set — skipping registration.");
  console.log("To rotate, remove it from .env and re-run.");
  process.exit(0);
}

async function main() {
  console.log("=== Circle Entity Secret Registration ===\n");

  const entitySecret = crypto.randomBytes(32).toString("hex");
  console.log("🔑 Generated entity secret:");
  console.log(`   ${entitySecret}\n`);

  console.log("Registering with Circle...");
  const response = await registerEntitySecretCiphertext({
    apiKey: API_KEY,
    entitySecret: entitySecret,
  });

  if (response.data?.recoveryFile) {
    const recoveryPath = path.join(import.meta.dirname, "recovery_file.dat");
    fs.writeFileSync(recoveryPath, response.data.recoveryFile);
    console.log(`📦 Recovery file saved to: wallet-setup/recovery_file.dat`);
    console.log("   ⚠️  SAVE THIS SECURELY - it can only be downloaded once!\n");
  }

  const envPath = path.resolve(import.meta.dirname, "../.env");
  let envContent = fs.existsSync(envPath) ? fs.readFileSync(envPath, "utf8") : "";
  envContent += `\nCIRCLE_ENTITY_SECRET=${entitySecret}\n`;
  fs.writeFileSync(envPath, envContent);
  console.log(`💾 Entity secret saved to root .env`);
  console.log("\n✅ Done! Next: make wallet");
}

main().catch((err) => {
  console.error("Failed:", err.message || err);
  // SDK 10.x throws HttpResponseError (status on the error, the axios error on
  // .error); 7.x threw the axios error itself. Read both shapes.
  const status = err.status ?? err.response?.status;
  const body = err.response?.data ?? err.error?.response?.data;
  if (status !== undefined) console.error("HTTP", status);
  if (body !== undefined) console.error("Body:", JSON.stringify(body, null, 2));
  process.exit(1);
});
