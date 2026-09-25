// Device payment key for Circle passkey (smart contract account) wallets —
// the #1467 rail.
//
// WHY THIS EXISTS. Circle's nanopayments facilitator validates x402 burn
// intents as plain EOA ERC-3009 signatures ONLY. Field-proven 2026-08-21
// against the live testnet facilitator:
//   - a deployed passkey SCA's ERC-1271 signature → invalid_signature;
//   - an on-chain-registered Gateway delegate (addDelegate confirmed,
//     isAuthorizedForBalance true, re-probed past the 5-minute validation
//     view window) signing for the depositor → invalid_signature;
//   - the same wire signed by the DEPOSITOR's own EOA key → verifies (and
//     settles when funded).
// Circle's docs match: "Nanopayments require an EOA wallet. Smart contract
// account (SCA) wallets are not supported"; ERC-1271 is excluded from
// nanopayments. So the only working shape is: an EOA is the Gateway
// DEPOSITOR and signs its own authorizations.
//
// THE DESIGN. Each device holds a locally generated "payment key" (a plain
// secp256k1 account). The passkey wallet funds it with ONE batched user
// operation — USDC.approve + GatewayWallet.depositFor(token, paymentKey,
// amount) — a single passkey prompt. The payment key then signs each $2
// authorization locally: no WebAuthn ceremony, no user-activation
// constraint, zero prompts per payment. It never holds gas and never sends
// an on-chain transaction; its only powers are (a) signing burn intents
// against its OWN Gateway balance — bounded by what the user deposited —
// and (b) the SIWE link proof below.
//
// CUSTODY HONESTY. The key lives in localStorage in the clear. Anything
// that can read this origin's storage can spend the key's REMAINING Gateway
// balance (never the passkey wallet's own funds — those stay behind the
// enclave). Losing the device/storage strands the remainder. Both bounds
// are deliberate v1 trade-offs, surfaced in the pay panel copy; deposits
// default small.
//
// ACCOUNT BINDING. The generation paywall requires authorization.from to be
// a wallet LINKED to the paying account (enforce_generation_payment). The
// payment key links itself through the normal SIWE challenge/verify flow —
// it is a real linked wallet, visible and removable in Account settings
// (provider "headless": a programmatic signer, which is exactly what it is).

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { apiPost } from "./api";
import { EXECUTION_CHAIN_ID } from "./chain-config";
import { listLinkedWallets } from "./linked-wallets";
import { canStore } from "./storage-consent.js";

const STORAGE_PREFIX = "archimedes_payment_key:";

const storageKey = (scaAddress) => `${STORAGE_PREFIX}${(scaAddress || "").toLowerCase()}`;

/** The stored payment-key account for this SCA, or null if none exists. */
export function getSessionAccount(scaAddress) {
	if (!scaAddress) return null;
	try {
		const stored = localStorage.getItem(storageKey(scaAddress));
		return stored ? privateKeyToAccount(stored) : null;
	} catch {
		return null;
	}
}

/** The stored payment-key account, generating and persisting one if absent. */
export function getOrCreateSessionAccount(scaAddress) {
	if (!scaAddress) throw new Error("No passkey wallet connected.");
	const existing = getSessionAccount(scaAddress);
	if (existing) return existing;
	const key = generatePrivateKey();
	// Strictly necessary (#1647 anti-goal 1): this IS the payment rail's
	// signing credential, so it is disclosed in the storage inventory with
	// its custody caveat rather than offered as a toggle that would silently
	// break paying. canStore therefore always allows it here.
	if (canStore(storageKey(scaAddress))) localStorage.setItem(storageKey(scaAddress), key);
	return privateKeyToAccount(key);
}

/** Forget the payment key for this SCA (the linked-wallet row and any
 * remaining Gateway balance under the key's address are NOT touched). */
export function clearSessionAccount(scaAddress) {
	try {
		localStorage.removeItem(storageKey(scaAddress));
	} catch {
		// Storage unavailable — nothing to clear.
	}
}

/**
 * Ensure the payment key is a linked wallet of the signed-in account, via
 * the same SIWE challenge/verify flow every wallet uses. Idempotent: checks
 * the linked list first. Signing is local (no prompt); the server stores
 * the link only after verifying the signature over ITS OWN challenge
 * message, same trust story as any wallet link.
 */
export async function ensureSessionLinked(account) {
	const wallets = await listLinkedWallets();
	const addr = account.address.toLowerCase();
	if ((Array.isArray(wallets) ? wallets : []).some((w) => (w?.address || "").toLowerCase() === addr)) {
		return;
	}
	const challenge = await apiPost("/api/wallets/challenge", {
		address: account.address,
		chain_id: EXECUTION_CHAIN_ID,
		provider: "headless",
	});
	const signature = await account.signMessage({ message: challenge.message });
	await apiPost("/api/wallets/verify", { message: challenge.message, signature });
}
