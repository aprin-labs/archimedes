// Pure helpers and copy for #1367's Delete-account control. Kept out of
// AccountSettings.jsx so they are directly unit-testable under plain `node
// --test` (.jsx is not importable there — see account-linking.js's header
// comment for the same reason).
//
// ── Why the lists below are data, not prose in the JSX ──────────────────
//
// "Delete my account" is a claim, and the thing that actually honours it is
// the DATABASE, not this file and not the auth sidecar: better-auth's
// /delete-user ends in a bare `DELETE FROM auth_users WHERE id = ?`
// (better-auth/dist/db/internal-adapter.mjs:148-161), and every consequence
// of that statement is decided by migration `85ca5310b7a1`'s per-table
// `ON DELETE` actions plus its `trg_auth_users_purge_unclaimed_owned_rows`
// trigger (see backend/tests/test_account_deletion_cascade.py, which drives
// exactly that statement).
//
// So a hand-written paragraph saying "we erase X and detach Y" is a claim
// nothing keeps true: change one `ondelete=` in a model and the paragraph
// silently becomes a lie. Structuring it as a table→sentence map lets
// ui/test/account-deletion.test.js read the ownership FKs straight out of
// backend/archimedes/models/*.py and fail when the two disagree — the same
// cross-language source-mirror idiom password-rules.test.js uses to keep the
// UI's password rules equal to auth/auth.js's.

// Rows that go away entirely, because their FK to auth_users.id is
// ON DELETE CASCADE. `user_profiles` is the one that matters most for
// privacy: it is where the Fernet-encrypted contact email lives.
export const DELETION_ERASED = [
  { table: 'auth_sessions', label: 'every signed-in session, on every device' },
  { table: 'api_keys', label: 'every API key minted for this account — revoked and erased; any script still holding one gets 401 on its next call' },
  { table: 'auth_accounts', label: 'your sign-in methods — the password itself, and any linked Google or GitHub' },
  { table: 'linked_wallets', label: 'your wallet links (the account↔wallet binding here, never anything on-chain)' },
  { table: 'wallet_link_challenges', label: 'any wallet-link challenge still outstanding' },
  { table: 'user_profiles', label: 'your profile row, including the encrypted contact email stored in it' },
  { table: 'paper_deployments', label: 'your paper-trading deployments and the daily returns recorded under them' },
  { table: 'auth_email_deliveries', label: 'the record of verification and password-reset emails we sent to your address, including the address itself' },
]

// Rows that survive with `owner_user_id` set to NULL, because other
// accounts can reference them by id — deleting them would break someone
// else's data, not just yours. The migration's own rationale, kept in step
// with it by the test.
export const DELETION_DETACHED = [
  { table: 'strategy_store', label: 'strategies you generated' },
  { table: 'strategy_passports', label: 'their rigor passports (audit records of what the gate decided)' },
  { table: 'strategy_proposals', label: 'the generation records behind them' },
  { table: 'vault_metadata', label: 'descriptions of vaults you created' },
]

// Rows that are NOT reached at all: these carry a `user_id` string with no
// foreign key to auth_users, so nothing in the database removes them when
// the account row goes. Naming them is the honest half of this list —
// omitting them would make the two lists above read as exhaustive when they
// are not. Whether financial records should be erased on request is a call
// for the people who own the money path; migration `85ca5310b7a1` says so
// explicitly and leaves both columns alone.
export const DELETION_RETAINED = [
  { table: 'payment_receipts', label: 'receipts for generations you paid for' },
  { table: 'generation_credits', label: 'the generation-credit ledger those payments wrote' },
]

// The typed confirmation. A `window.confirm` alone is one keystroke away
// from an irreversible, unrecoverable action (AccountSettings.jsx already
// uses bare confirms for wallet/provider unlink, which are recoverable by
// re-linking — this is not). Matching is whitespace-tolerant and
// case-insensitive on purpose: the goal is to force the user to READ and
// retype a sentence, not to test their shift key, and an autocapitalising
// mobile keyboard turning "delete" into "Delete" is not a signal that they
// meant something different.
export const DELETE_CONFIRMATION_PHRASE = 'delete my account'

export function deleteConfirmationMatches(input) {
  return typeof input === 'string' && input.trim().toLowerCase() === DELETE_CONFIRMATION_PHRASE
}

// Better Auth's /delete-user takes the password branch only when the
// account actually HAS a credential row; passing a password to a
// Google/GitHub-only account is a 400 CREDENTIAL_ACCOUNT_NOT_FOUND, and
// omitting it on an account that has one falls through to the freshness
// check instead of the re-authentication the UI just performed. So which
// branch to take is decided by the connected-accounts list, not by whether
// the user typed something.
export function accountHasPassword(connectedAccounts) {
  return Array.isArray(connectedAccounts) && connectedAccounts.some((account) => account?.providerId === 'credential')
}
