// Circle Modular Wallets SDK integration — passkey-based smart contract
// accounts (MSCA) on Arc Testnet. Works in any modern browser (Safari,
// Chrome, Firefox) since it uses the standard WebAuthn API instead of an
// injected wallet extension.
//
// Two flows:
//   - Register: first-time user creates a P256 credential on their device
//     (Face ID / Touch ID / Windows Hello / hardware key). The MSCA is
//     derived deterministically from the public key.
//   - Login: returning user re-authenticates with the same credential to
//     unlock the same MSCA address.
//
// MSCAs are LAZILY DEPLOYED — gas for account creation is deferred until
// the first outbound user operation. The Arc Testnet path is `/arcTestnet`.
//
// Reference: submodules/context-arc/docs/circlefin-skills/use-modular-wallets.md
// Setup: ui/.env.example documents VITE_CIRCLE_CLIENT_KEY + VITE_CIRCLE_CLIENT_URL.

import { createPublicClient } from 'viem'
import { EXECUTION_CHAIN_ID } from './chain-config'
import { toWebAuthnAccount } from 'viem/account-abstraction'
import {
  toWebAuthnCredential,
  toPasskeyTransport,
  toModularTransport,
  toCircleSmartAccount,
  WebAuthnMode,
} from '@circle-fin/modular-wallets-core'
import { canStore } from './storage-consent.js'

const CLIENT_KEY = import.meta.env.VITE_CIRCLE_CLIENT_KEY ?? ''
const CLIENT_URL = import.meta.env.VITE_CIRCLE_CLIENT_URL
  ?? 'https://modular-sdk.circle.com/v1/rpc/w3s/buidl'

// Arc Testnet chain definition; matches the EOA-path chain in config.js so
// any downstream code that introspects .chain sees the same object shape.
const arcTestnet = {
  id: EXECUTION_CHAIN_ID,
  name: 'Arc Testnet',
  nativeCurrency: { name: 'USD Coin', symbol: 'USDC', decimals: 18 },
  rpcUrls: { default: { http: ['https://rpc.testnet.arc.network'] } },
}

// Demo-grade persistence. Per Circle docs, production should use httpOnly
// cookies to mitigate XSS credential theft — out of scope for hackathon demo.
const CREDENTIAL_STORAGE_KEY = 'archimedes_circle_credential'
// Legacy: older builds persisted a per-device username here. Registration now
// generates a fresh unique username per wallet (newUniqueUsername) and login is
// discoverable (no username), so nothing is written here anymore — the key is
// retained only so clearCircleSession() can purge values left by old builds.
const USERNAME_STORAGE_KEY = 'archimedes_circle_username'

// CREDENTIAL_STORAGE_KEY is localStorage, so it's shared across every tab of
// this origin — if Tab B registers/connects a different passkey wallet, Tab A
// would otherwise silently rehydrate as that new identity on its next reload.
// sessionStorage IS tab-scoped, so we stamp the credential id this tab last
// saw/used here; a mismatch on rehydrate means another tab changed the wallet
// underneath this one (see rehydrateSmartAccount below).
const TAB_SEEN_CREDENTIAL_ID_KEY = 'archimedes_circle_credential_tab_seen_id'

function getTabSeenCredentialId() {
  try { return sessionStorage.getItem(TAB_SEEN_CREDENTIAL_ID_KEY) } catch { return null }
}

function setTabSeenCredentialId(id) {
  try {
    // Strictly necessary (#1647): this stamp is the guard against signing as
    // a wallet another tab swapped in, so canStore always allows it. Gated
    // anyway so no write in ui/src is ungated.
    if (id && canStore(TAB_SEEN_CREDENTIAL_ID_KEY)) sessionStorage.setItem(TAB_SEEN_CREDENTIAL_ID_KEY, id)
    else if (!id) sessionStorage.removeItem(TAB_SEEN_CREDENTIAL_ID_KEY)
  } catch { /* storage unavailable */ }
}

// True if a CLIENT_KEY was supplied at build time. The modal hides the
// passkey option when this is false so we never show a broken button.
export function circlePasskeyEnabled() {
  return Boolean(CLIENT_KEY)
}

function loadStoredCredential() {
  try {
    const raw = localStorage.getItem(CREDENTIAL_STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

function saveCredential(credential) {
  try {
    // Strictly necessary (#1647): the passkey wallet cannot be reconnected or
    // used to sign without it, so it is never offered as a toggle.
    if (canStore(CREDENTIAL_STORAGE_KEY)) localStorage.setItem(CREDENTIAL_STORAGE_KEY, JSON.stringify(credential))
  } catch { /* storage unavailable */ }
  // This tab is the one that just wrote the credential, so it's the source of
  // the change, not a victim of drift — establish it as this tab's baseline.
  setTabSeenCredentialId(credential?.id ?? null)
}

// Subscribe to cross-tab credential changes. The native `storage` event only
// fires in OTHER tabs of the same origin (never the tab that made the write),
// which makes it the standard browser primitive for detecting "another tab
// just swapped the passkey wallet" without waiting for this tab to reload.
// Returns an unsubscribe function. No-op (returns a no-op unsubscribe) outside
// a browser context.
export function onCredentialChange(callback) {
  if (typeof window === 'undefined' || typeof callback !== 'function') return () => {}
  const handler = (event) => {
    if (event.key !== CREDENTIAL_STORAGE_KEY) return
    let credential = null
    try { credential = event.newValue ? JSON.parse(event.newValue) : null } catch { /* */ }
    callback({ credential })
  }
  window.addEventListener('storage', handler)
  return () => window.removeEventListener('storage', handler)
}

// A fresh, UNIQUE username for each Register (new wallet). Circle's server
// rejects duplicate usernames, so reusing one would block creating a second
// wallet. Format: `archimedes-<12 hex chars>` — within Circle's 5-50 char and
// [a-zA-Z0-9_@.:+-] constraints. Not persisted: discoverable login needs no
// username, so there is nothing to remember between sessions.
function newUniqueUsername() {
  // Cryptographically secure 12-hex-char suffix. `crypto` is always present in
  // the secure contexts where WebAuthn works, so randomUUID is the happy path;
  // getRandomValues is the fallback for engines lacking randomUUID. No
  // Math.random fallback — it is not cryptographically secure and CodeQL
  // (js/insecure-randomness) rightly flags it in this credential-creation path.
  const uuid = crypto.randomUUID?.()
  if (uuid) return `archimedes-${uuid.replace(/-/g, '').slice(0, 12)}`
  const bytes = crypto.getRandomValues(new Uint8Array(6))
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `archimedes-${hex}`
}

// Turn a user-typed wallet name into a Circle-valid username (5-50 chars from
// [a-zA-Z0-9_@.:+-]): trim, collapse disallowed runs (incl. spaces) to a single
// hyphen, strip edge hyphens, cap at 50. Returns null when the input is empty or
// sanitizes to fewer than Circle's 5-char floor, so the caller falls back to an
// auto-generated unique name. Circle enforces username uniqueness, so the name
// doubles as the human-readable label shown in the passkey picker at login.
export function sanitizeWalletName(name) {
  if (!name) return null
  const cleaned = String(name)
    .trim()
    .replace(/[^a-zA-Z0-9_@.:+-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 50)
  return cleaned.length >= 5 ? cleaned : null
}

// UI-facing display name for a newly registered wallet: the user's typed name,
// trimmed and capped at 40 chars. Distinct from sanitizeWalletName above, which
// maps the name into Circle's username charset for the OS passkey-picker label —
// the display name keeps spaces/punctuation so the app UI shows what the user
// actually typed. Returns undefined for empty/blank input so callers fall
// through to the address-based label.
const DISPLAY_NAME_MAX_CHARS = 40

function toDisplayWalletName(name) {
  if (typeof name !== 'string') return undefined
  const trimmed = name.trim().slice(0, DISPLAY_NAME_MAX_CHARS)
  return trimmed.length > 0 ? trimmed : undefined
}

export function clearCircleSession() {
  try {
    localStorage.removeItem(CREDENTIAL_STORAGE_KEY)
    localStorage.removeItem(USERNAME_STORAGE_KEY)
  } catch { /* */ }
  setTabSeenCredentialId(null)
}

// Returns true if a previously-registered passkey is on file for this
// origin. Cheap synchronous check used to decide whether the modal should
// show "Sign in with Passkey" (returning) vs "Create Passkey" (new user).
export function hasStoredPasskey() {
  return loadStoredCredential() !== null
}

// Map raw passkey errors (viem RPC errors, DOMExceptions) to user-readable
// strings. The default error.message for viem is "An unknown RPC error
// occurred. Details: …. Version: viem@x.y.z" — informative for debugging,
// hostile for the user. This converts the common cases.
function friendlyPasskeyError(err) {
  const msg = String(err?.message ?? err ?? '')
  if (/NotAllowedError/i.test(msg) || /not allowed/i.test(msg)) {
    return 'Sign-in cancelled or no passkey selected.'
  }
  if (/SecurityError/i.test(msg)) {
    return 'Passkey blocked: this site\'s domain doesn\'t match the registered passkey origin.'
  }
  if (/username is duplicated/i.test(msg)) {
    return 'A passkey for this site already exists on Circle\'s server but no passkey matched on your device. Use the device that originally created it, or click "Clear and try again" to register fresh.'
  }
  if (/no credentials available/i.test(msg) || /no passkey/i.test(msg)) {
    return 'No passkey found on this device. Create a new one to continue.'
  }
  return msg || 'Passkey sign-in failed.'
}

// Connect a Circle Modular Wallet via passkey. Two EXPLICIT flows — the caller
// chooses; we never auto-decide (auto-deciding is what minted a brand-new
// wallet on every fresh browser, since "no cached credential" fell through to
// Register):
//
//   - mode 'login'    → DISCOVERABLE login. Omit both username and credentialId
//     so the browser shows its native passkey picker listing EVERY passkey
//     registered for this origin. The user picks one; its public key
//     deterministically derives that wallet's MSCA address. This is how a user
//     signs back into a SPECIFIC existing wallet (and recovers it on a fresh
//     browser / device).
//   - mode 'register' → CREATE a new wallet: register a fresh passkey under the
//     user-chosen `walletName` (sanitized to Circle's username rules), or an
//     auto-generated unique name if none is given. Circle rejects duplicate
//     usernames. New credential → new MSCA address.
//
// Returns the smart account + address + credential (persisted to localStorage
// for silent next-session rehydrate — see rehydrateSmartAccount). Register mode
// also echoes the user-typed wallet name (trimmed, ≤40 chars) as `walletName`
// so the caller can persist it for the app UI; login mode returns undefined —
// discoverable login can't know which passkey the user picked. Throws a
// friendly message on WebAuthn cancellation / domain mismatch / name collision.
export async function connectCirclePasskey({ mode = 'login', walletName } = {}) {
  if (!circlePasskeyEnabled()) {
    throw new Error('Circle passkey wallet is not configured (missing VITE_CIRCLE_CLIENT_KEY).')
  }

  // Passkey transport handles WebAuthn challenge issuance + verification.
  const passkeyTransport = toPasskeyTransport(CLIENT_URL, CLIENT_KEY)

  // Register uses the user-chosen name (sanitized) so the wallet is recognizable
  // in the passkey picker at login; falls back to an auto-generated unique name.
  const registerUsername = sanitizeWalletName(walletName) ?? newUniqueUsername()

  // Browser prompts the user for biometrics here. Register issues a new P256
  // credential under that username; Login is discoverable (no username, no
  // credentialId) so the browser surfaces all passkeys for this origin to pick.
  let credential
  try {
    credential = await toWebAuthnCredential(
      mode === 'register'
        ? { transport: passkeyTransport, mode: WebAuthnMode.Register, username: registerUsername }
        : { transport: passkeyTransport, mode: WebAuthnMode.Login },
    )
  } catch (err) {
    // A custom name that collides with an existing wallet's name gets a clear,
    // actionable message instead of the generic duplicate-credential copy.
    const raw = String(err?.message ?? err ?? '')
    if (mode === 'register' && sanitizeWalletName(walletName) && /username is duplicated/i.test(raw)) {
      throw new Error('That wallet name is already taken — choose a different one.', { cause: err })
    }
    throw new Error(friendlyPasskeyError(err), { cause: err })
  }

  // Persist the credential (id + public key, NOT the private key — that stays
  // in the device's secure enclave) so we can silently rehydrate next session.
  saveCredential(credential)

  // Modular transport handles bundler RPC + chain-specific calls for the
  // smart account on Arc Testnet.
  const modularTransport = toModularTransport(`${CLIENT_URL}/arcTestnet`, CLIENT_KEY)
  const client = createPublicClient({ chain: arcTestnet, transport: modularTransport })

  // Derive a WebAuthnAccount viem account from the credential, then turn it
  // into a Circle smart account. The MSCA address is deterministic from the
  // credential's public key — same passkey → same wallet address.
  const owner = toWebAuthnAccount({ credential })
  const smartAccount = await toCircleSmartAccount({ client, owner })

  return {
    address: smartAccount.address,
    smartAccount,
    client,
    credential,
    mode,
    // Register mode: the user-typed name (trimmed, ≤40 chars) for app-UI
    // persistence; undefined when blank or in login mode (discoverable login
    // doesn't reveal which passkey was picked).
    walletName: mode === 'register' ? toDisplayWalletName(walletName) : undefined,
  }
}

// Rebuild the smart account from a previously-stored credential WITHOUT
// triggering a WebAuthn prompt. Safe to call on every page load — the
// credential only contains the public key (the private key lives in the
// device's secure enclave) so we can derive the smart account address
// + signer wrapper without re-authenticating. The user only sees a
// WebAuthn prompt when they actually try to sign a user operation.
//
// Returns null if there's no stored credential (caller should redirect
// to the register flow).
//
// Cross-tab drift: CREDENTIAL_STORAGE_KEY is localStorage (shared across every
// tab of this origin). If this tab previously saw a credential and the one now
// on file has a different id, another tab registered/switched to a different
// passkey wallet since — this tab would otherwise silently rehydrate as that
// new identity with no warning. `credentialChanged: true` on the return value
// surfaces that so a caller can require explicit user re-confirmation instead
// of proceeding as usual; callers that don't check it keep today's behavior.
export async function rehydrateSmartAccount() {
  if (!circlePasskeyEnabled()) return null
  const credential = loadStoredCredential()
  if (!credential) return null

  const seenId = getTabSeenCredentialId()
  const credentialChanged = Boolean(seenId && credential.id && seenId !== credential.id)

  const modularTransport = toModularTransport(`${CLIENT_URL}/arcTestnet`, CLIENT_KEY)
  const client = createPublicClient({ chain: arcTestnet, transport: modularTransport })

  const owner = toWebAuthnAccount({ credential })
  const smartAccount = await toCircleSmartAccount({ client, owner })

  setTabSeenCredentialId(credential.id)

  return {
    address: smartAccount.address,
    smartAccount,
    client,
    credential,
    credentialChanged,
  }
}
