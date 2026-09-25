import { useCallback, useEffect, useRef, useState } from 'react'

import {
  accountHasPassword,
  DELETE_CONFIRMATION_PHRASE,
  deleteConfirmationMatches,
  DELETION_DETACHED,
  DELETION_ERASED,
  DELETION_RETAINED,
} from '../account-deletion'
import { canUnlink, connectableProvidersIntro, connectedActionErrorState, connectedProviderLabel, isLinkableProvider } from '../account-linking'
import { linkErrorMessage } from '../auth-errors'
import { getProviders, linkSocial, listAccounts, unlinkAccount } from '../auth-client'
import { useAuth } from '../AuthContext'
import { canStore } from '../storage-consent.js'
import {
  changeEmail,
  changePassword,
  deleteAccount,
  getVerificationStatus,
  listSessions,
  resendVerificationEmail,
  revokeOtherSessions,
  revokeSession,
} from '../auth-client'
import VerificationDeliveryStatus from './VerificationDeliveryStatus'
import { deriveVerificationDeliveryView, RATE_LIMITED_BY_CLIENT, shouldShowRequestedFallback } from '../verificationDelivery'
import { PASSWORD_MIN, passwordRulesMet, passwordsMatch } from '../password-rules'
import { listLinkedWallets, makePrimaryWallet, removeLinkedWallet } from '../linked-wallets'
import { providerLabel } from '../wallet-providers'

// Round-3 review finding (minor): a one-shot, client-set marker naming
// which provider `link()` below is ABOUT to redirect for. The `?linked=`
// notice effect only fires when this matches the URL's `linked` value, and
// clears it on the very first read (success or not) — so neither a bare
// `?linked=<provider>` a user is handed nor an old link's callback URL
// bookmarked/replayed later can trigger the toast; only a redirect this tab
// itself just initiated through the Link button can.
const PENDING_LINK_KEY = 'archimedes:pending-link'

// Better Auth's /send-verification-email returns {status:true} once the send is
// QUEUED, not once it is delivered — and the auth sidecar's mailer fail-softs on
// error (auth/auth.js) precisely because SES is sandboxed today, so mail to a
// non-SES-verified address can silently never arrive. Copy here must never claim
// delivery, only that a send was requested.
const VERIFICATION_REQUESTED_MESSAGE = "Verification email requested — delivery isn't confirmed and may take a few minutes."

// #1367: one message for every outcome of an email-change request, on
// purpose. Better Auth answers an address that already belongs to another
// account with the same `{status:true}` it answers a free one, and sends
// nothing (better-auth/dist/api/routes/update-user.mjs:456-460) — branching
// this copy on the result would hand any signed-in visitor an
// account-existence oracle the server deliberately refuses to give. Same
// discipline as AuthPage.jsx's RESET_REQUESTED_MESSAGE.
//
// It also must not claim delivery (mail is fail-soft while SES is
// sandboxed — see auth/auth.js) and must not claim the address changed,
// because it has not: the switchover happens when the link in the NEW
// address's message is opened, and not before.
const EMAIL_CHANGE_REQUESTED_MESSAGE
  = 'Requested. If that address is usable, a confirmation link is on its way — delivery '
  + "isn't confirmed and may take a few minutes. Your email address does not change until "
  + 'the new address confirms it.'

const PASSWORD_CHANGED_MESSAGE = 'Password changed. Every other signed-in session was ended.'

// Deliberately NOT auth-errors.js's SESSION_NOT_FRESH copy, which names
// "changing sign-in methods" — the trigger here is listing sessions, and
// naming the wrong action would be a small lie in the one place the user is
// trying to work out whether someone else is signed in. Revoking is not
// gated the same way (session.mjs uses sensitiveSessionMiddleware there,
// not freshSessionMiddleware), so the "end all other sessions" control
// deliberately stays live in this state — it is the one thing a worried
// user most needs to still work.
const SESSIONS_STALE_MESSAGE
  = 'For security, listing your sessions needs a sign-in from the last 24 hours. '
  + 'You can still end every other session below.'

// Better Auth's /list-sessions row carries no "this is you" marker, so the
// current session is identified by comparing against the session the app is
// already holding (AuthContext's `session`) rather than by guessing from
// recency or user-agent.
function isCurrentSession(session, currentSession) {
  return Boolean(currentSession?.id) && session?.id === currentSession.id
}

function formatSessionTimestamp(value) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

export default function AccountSettings({ walletAddr, onDisconnect, linkError }) {
  const { user, session: currentSession, signOut } = useAuth()
  const [wallets, setWallets] = useState([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(null)
  const [verifyStatus, setVerifyStatus] = useState('idle') // idle | sending | sent | error
  const [verifyError, setVerifyError] = useState('')
  // #1748 item 2 — the auth service's own record of what happened to this
  // address's verification mail. `null` until the first successful read, and
  // back to `null` on any failure: an unreachable status endpoint renders
  // nothing, never an optimistic default.
  const [verifyDelivery, setVerifyDelivery] = useState(null)

  // #1367 — email change
  const [newEmail, setNewEmail] = useState('')
  const [emailBusy, setEmailBusy] = useState(false)
  const [emailNotice, setEmailNotice] = useState('')
  const [emailError, setEmailError] = useState('')

  // #1367 — password change
  const [passwordForm, setPasswordForm] = useState({ current: '', next: '', confirm: '' })
  const [passwordBusy, setPasswordBusy] = useState(false)
  const [passwordNotice, setPasswordNotice] = useState('')
  const [passwordError, setPasswordError] = useState('')

  // #1367 — active sessions
  const [sessions, setSessions] = useState([])
  const [sessionsLoaded, setSessionsLoaded] = useState(false)
  const [sessionsStale, setSessionsStale] = useState(false)
  const [sessionsError, setSessionsError] = useState('')
  const [sessionsNotice, setSessionsNotice] = useState('')
  const [sessionBusy, setSessionBusy] = useState(null)

  // #1367 — account deletion
  const [deletePhrase, setDeletePhrase] = useState('')
  const [deletePassword, setDeletePassword] = useState('')
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState('')

  const [connectedAccounts, setConnectedAccounts] = useState([])
  const [connectedLoaded, setConnectedLoaded] = useState(false)
  const [connectedProviders, setConnectedProviders] = useState({ google: false, github: false })
  const [connectedProvidersError, setConnectedProvidersError] = useState(false)
  const [connectedError, setConnectedError] = useState('')
  const [connectedNotice, setConnectedNotice] = useState('')
  const [connectedBusy, setConnectedBusy] = useState(null)
  // Set once a link/unlink attempt discovers the session is too old to
  // change connected accounts (round-2 review, blocker: auth.js now gates
  // BOTH /link-social and /unlink-account on session freshness — see
  // auth/auth.js's hooks.before). Client-side we only learn this
  // reactively, from a 403 — there is no lighter-weight signal to poll
  // ahead of a click — so once discovered it disables the whole section
  // and offers the one way out, instead of leaving live-looking buttons
  // that will just 403 again.
  const [connectedSessionStale, setConnectedSessionStale] = useState(false)

  const load = useCallback(() => listLinkedWallets().then(setWallets).catch((err) => setError(err.message)), [])
  useEffect(() => { load() }, [load])

  // connectedLoaded is tracked separately from connectedAccounts.length so
  // "Loading…" cannot render forever: before this fix, a rejected
  // listAccounts() left connectedAccounts at its initial `[]`, which the
  // render below was (mis)using as the loading sentinel — the same empty
  // state a successful-but-truly-empty load would also produce (round-2
  // review finding). Both branches below now set connectedLoaded so the
  // request always resolves out of the loading state, error or not.
  const loadConnected = useCallback(
    () => listAccounts()
      .then((accounts) => { setConnectedAccounts(accounts); setConnectedLoaded(true) })
      .catch((err) => { setConnectedError(err.message); setConnectedLoaded(true) }),
    [],
  )
  useEffect(() => { loadConnected() }, [loadConnected])

  // #1367: /list-sessions is gated behind Better Auth's OWN
  // freshSessionMiddleware (better-auth/dist/api/routes/session.mjs:378),
  // unlike /revoke-session and /revoke-other-sessions, which are not. With
  // sessions living 7 days and freshAge at 24h (auth/auth.js), a stale
  // session therefore CANNOT list its own sessions — and that must render
  // as the honest re-auth state, never as "no other sessions", which is the
  // same empty shape a genuinely single-session account produces. Same
  // attempt-then-react-to-the-server contract as the connected-accounts
  // section: no client-side clock arithmetic anywhere in this file.
  const loadSessions = useCallback(
    () => listSessions()
      .then((rows) => { setSessions(Array.isArray(rows) ? rows : []); setSessionsStale(false); setSessionsLoaded(true) })
      .catch((err) => {
        const { stale, message } = connectedActionErrorState(err)
        setSessionsStale(stale)
        setSessionsError(stale ? SESSIONS_STALE_MESSAGE : message)
        setSessionsLoaded(true)
      }),
    [],
  )
  useEffect(() => { loadSessions() }, [loadSessions])
  // Round-3 review finding (minor): a rejected getProviders() used to be
  // swallowed with an empty no-op catch handler — indistinguishable from the
  // legitimate "no OAuth providers configured" state, so a real fetch
  // failure silently hid the Link controls while the section's own copy
  // ("Google and GitHub link only after you authorize them from here...")
  // kept promising an affordance that had quietly gone missing. Now
  // surfaced through the same connectedError alert a link/unlink failure
  // uses, instead of a bare empty catch.
  useEffect(() => {
    getProviders()
      .then((providers) => setConnectedProviders({ google: !!providers.google, github: !!providers.github }))
      .catch(() => setConnectedProvidersError(true))
  }, [])

  // Read the explicit-link round trip's success marker off the URL, once,
  // after the first successful account load — same local pattern
  // AuthPage.jsx uses for its reset-password token. Round-2 review finding:
  // this used to fire straight off the URL with no check at all, so it
  // could assert a link that never happened (any `?linked=` value rendered
  // verbatim) instead of being the confirmation toast its own comment
  // claimed it was. Fixed to require both a recognized provider AND
  // presence in the freshly reloaded connectedAccounts list — but round-3
  // review found `isKnownConnectedProvider` still let `?linked=credential`
  // through (present in connectedAccounts for every password user, never
  // something these Link buttons produce) and let a stale/shared
  // `?linked=google` re-assert a link from the past. Now gated on THREE
  // things: isLinkableProvider (only google/github — the providers this UI
  // can actually initiate), presence in connectedAccounts (the real proof
  // something is linked), AND the one-shot PENDING_LINK_KEY marker `link()`
  // sets immediately before its own redirect — so the toast can only ever
  // fire for a link this tab itself just initiated, not an arbitrary or
  // replayed URL.
  const linkedNoticeChecked = useRef(false)
  useEffect(() => {
    if (!connectedLoaded || linkedNoticeChecked.current) return
    linkedNoticeChecked.current = true
    const linked = new URLSearchParams(window.location.search).get('linked')
    const pendingLink = sessionStorage.getItem(PENDING_LINK_KEY)
    sessionStorage.removeItem(PENDING_LINK_KEY)
    if (!linked || !isLinkableProvider(linked) || linked !== pendingLink) return
    if (connectedAccounts.some((account) => account.providerId === linked)) {
      setConnectedNotice(`Linked ${connectedProviderLabel(linked)}.`)
    }
  }, [connectedLoaded, connectedAccounts])

  const linkErrorNotice = linkErrorMessage(linkError)
  // Round-4 review finding (minor): derived from the SAME provider-discovery
  // result that gates the Link buttons themselves (connectedProviders,
  // fetched above), so the intro copy can never promise a provider the
  // buttons below it don't actually offer — see connectableProvidersIntro's
  // own header comment in account-linking.js.
  const providersIntro = connectableProvidersIntro(connectedProviders)

  // Round-4 review finding (major): link() and unlinkConnected() below both
  // hit this exact fork — ATTEMPT the action, then react to whatever the
  // server actually says, never precompute session age client-side (the
  // server is the sole freshness authority; see account-linking.js's header
  // comment on connectedActionErrorState). Shared here so both catches stay
  // identical and the mapping itself is unit-tested directly.
  const handleConnectedActionError = (err) => {
    const { stale, message } = connectedActionErrorState(err)
    if (stale) setConnectedSessionStale(true)
    // SESSION_NOT_FRESH always renders the one honest, actionable message
    // from auth-errors.js — never the raw library string, and never a
    // fabricated notice/success state alongside it (connectedNotice is
    // cleared before the request above and never set in this branch).
    setConnectedError(stale ? linkErrorMessage('SESSION_NOT_FRESH') : message)
  }

  const link = async (provider) => {
    setConnectedBusy(provider)
    setConnectedError('')
    setConnectedNotice('')
    try {
      const origin = window.location.origin
      // Set the one-shot pending-link marker right before the redirect — see
      // PENDING_LINK_KEY above. Both callbacks land back on THIS page — the
      // /callback/:id endpoint (library-managed CSRF state, not this code)
      // is what actually decides success/failure before either is reached.
      // Strictly necessary (#1647): this is the anti-replay check on the
      // linking flow, so canStore always allows it. Gated anyway so that no
      // write in ui/src is ungated.
      if (canStore(PENDING_LINK_KEY)) sessionStorage.setItem(PENDING_LINK_KEY, provider)
      await linkSocial(provider, `${origin}/app/account?linked=${provider}`, `${origin}/app/account`)
    } catch (err) {
      // The redirect never happened — clear the marker so a later, unrelated
      // `?linked=${provider}` (e.g. the same URL pasted back in) can't be
      // mistaken for the outcome of this failed attempt.
      sessionStorage.removeItem(PENDING_LINK_KEY)
      handleConnectedActionError(err)
      setConnectedBusy(null)
    }
  }

  // Guarded twice on purpose: canUnlink() keeps the button disabled/inert in
  // the last-credential state (see the export above), and the confirm below
  // still runs before the network call for any account, mirroring the
  // wallet-unlink confirm pattern above (3.3.4 — no single unconfirmed click
  // for a destructive, no-undo action). Round-4 review finding (major): this
  // confirm can still fire on a stale session (the server, not client-guessed
  // age, is what actually knows) — that is correct per the "attempt, then
  // react to the server" contract above, not a bug to route around; a
  // 403 after "OK" still lands on the same honest re-auth state as Link's.
  const unlinkConnected = async (account) => {
    if (!canUnlink(connectedAccounts.length)) return
    const label = connectedProviderLabel(account.providerId)
    if (!window.confirm(`Unlink ${label}? You will no longer be able to sign in with ${label}.`)) return
    setConnectedBusy(account.id)
    setConnectedError('')
    setConnectedNotice('')
    try {
      await unlinkAccount(account.providerId, account.accountId)
      await loadConnected()
      setConnectedNotice(`Unlinked ${label}.`)
    } catch (err) {
      handleConnectedActionError(err)
    } finally {
      setConnectedBusy(null)
    }
  }

  const primary = async (id) => {
    setBusy(id)
    setError('')
    try {
      await makePrimaryWallet(id)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  // Unlinking deletes the account↔wallet binding that owns the user's on-chain
  // history, with no undo — relinking means re-running the SIWE signature flow,
  // and until then the wallet's data is invisible. It was a single unconfirmed
  // click, so a mis-click or a stray Enter on a focused button silently
  // stranded strategies (3.3.4). PaperTrading.jsx already confirms the
  // comparable destructive action, so this was an inconsistency, not a policy.
  const unlink = async (wallet) => {
    const label = wallet.display_address || wallet.address
    if (!window.confirm(
      `Unlink ${label}? This removes the account↔wallet binding; re-linking requires signing again with that wallet.`,
    )) return
    setBusy(wallet.id)
    setError('')
    setNotice('')
    try {
      await removeLinkedWallet(wallet.id)
      if (walletAddr?.toLowerCase() === wallet.address) onDisconnect?.()
      await load()
      setNotice(`Unlinked ${label}.`)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  const logout = async () => {
    await signOut()
    onDisconnect?.()
    window.location.assign('/')
  }

// Round-4 review finding (major): the stale-session "Sign in again"
  // affordance below used to just call logout(), which lands on '/' —
  // landing has no sign-in form, so it dumped the user on the marketing
  // page with no path back here. AuthPage's own `user && window.location.
  // replace(next)` guard means a still-signed-in session bounces straight
  // past the sign-in form before it can render, so the (still valid, just
  // stale) session has to actually end — same as logout() — then land on
  // /sign-in with the same `?next=` convention every other anonymous bounce
  // in this app uses (routes.js's postAuthPath/safeNextPath; see Layout.jsx/
  // Leaderboard.jsx/StrategyPassport.jsx for the precedent), so completing
  // sign-in returns to Account Settings instead of the generic /app.
  const reauthenticate = async () => {
    await signOut()
    onDisconnect?.()
    window.location.assign(`/sign-in?next=${encodeURIComponent('/app/account')}`)
  }

  // #1748 item 2 — what the auth service actually recorded for this address.
  // Read on mount (unverified accounts only: a verified one has no pending
  // delivery to describe) and again after every resend click.
  const refreshVerifyDelivery = useCallback(async () => {
    try {
      setVerifyDelivery(await getVerificationStatus())
    } catch {
      // 401 (signed out), 503 (no delivery log wired), or a network failure.
      // None of those is knowledge about anyone's mail, so the panel shows
      // nothing rather than guessing.
      setVerifyDelivery(null)
    }
  }, [])

  useEffect(() => {
    if (user?.emailVerified === false) refreshVerifyDelivery()
  }, [user?.emailVerified, refreshVerifyDelivery])

  // On-demand resend: heals anyone who missed their verification window.
  // Does not gate anything — email-verification ENFORCEMENT stays off
  // (auth/auth.js requireEmailVerification). Until #1748 this button's only
  // possible answer was VERIFICATION_REQUESTED_MESSAGE, because Better Auth
  // returns {status:true} once the send is QUEUED and the mailer fail-softs;
  // the delivery panel below now carries what happened after that.
  const sendVerification = async () => {
    setVerifyStatus('sending')
    setVerifyError('')
    try {
      await resendVerificationEmail(user.email, `${window.location.origin}/app`)
      setVerifyStatus('sent')
      // Re-read AFTER the send so the panel reflects this click — including
      // the case where the send was accepted and then suppressed.
      await refreshVerifyDelivery()
    } catch (err) {
      setVerifyError(err.message)
      setVerifyStatus('error')
      // Better Auth's limiter is keyed per client IP, not per address, so a
      // 429 is a fact the server-side status cannot always see. Render it from
      // what this client just observed instead of leaving the old state up.
      if (err.status === 429) setVerifyDelivery(RATE_LIMITED_BY_CLIENT)
      else await refreshVerifyDelivery()
    }
  }

  // Suppressed and rate-limited are the two states where pressing the button
  // again cannot help. The control reads the SAME derivation the panel
  // renders, so the button and the copy under it can never disagree.
  const resendDisabled = verifyStatus === 'sending'
    || deriveVerificationDeliveryView(verifyDelivery)?.canResend === false

  // ── #1367: email change ───────────────────────────────────────────────
  // Never reads the response. See EMAIL_CHANGE_REQUESTED_MESSAGE for why the
  // same sentence has to follow every non-error outcome.
  const submitEmailChange = async (event) => {
    event.preventDefault()
    setEmailBusy(true)
    setEmailError('')
    setEmailNotice('')
    try {
      await changeEmail(newEmail.trim(), `${window.location.origin}/app/account`)
      setEmailNotice(EMAIL_CHANGE_REQUESTED_MESSAGE)
      setNewEmail('')
    } catch (err) {
      setEmailError(err.message)
    } finally {
      setEmailBusy(false)
    }
  }

  // ── #1367: password change ────────────────────────────────────────────
  const passwordReady
    = passwordForm.current.length > 0
    && passwordRulesMet(passwordForm.next)
    && passwordsMatch(passwordForm.next, passwordForm.confirm)

  const submitPasswordChange = async (event) => {
    event.preventDefault()
    setPasswordBusy(true)
    setPasswordError('')
    setPasswordNotice('')
    try {
      await changePassword(passwordForm.current, passwordForm.next)
      setPasswordForm({ current: '', next: '', confirm: '' })
      setPasswordNotice(PASSWORD_CHANGED_MESSAGE)
      // The rotation revoked every other session server-side, so the list
      // on this page is now wrong until it is refetched.
      await loadSessions()
    } catch (err) {
      setPasswordError(err.message)
    } finally {
      setPasswordBusy(false)
    }
  }

  // ── #1367: session revocation ─────────────────────────────────────────
  // A 200 from /revoke-session only means the session is gone because
  // auth/auth.js's hooks.before turns "that token isn't yours" into a 404
  // first — the library on its own answers 200 either way. Without that
  // guard the success notice below would be a claim the code does not back.
  const revokeOne = async (session) => {
    if (!window.confirm('End this session? Whoever is using it will have to sign in again.')) return
    setSessionBusy(session.id)
    setSessionsError('')
    setSessionsNotice('')
    try {
      await revokeSession(session.token)
      await loadSessions()
      setSessionsNotice('Session ended.')
    } catch (err) {
      setSessionsError(err.message)
    } finally {
      setSessionBusy(null)
    }
  }

  const revokeEveryOther = async () => {
    if (!window.confirm('End every other session? Every other device signed in as you will have to sign in again.')) return
    setSessionBusy('others')
    setSessionsError('')
    setSessionsNotice('')
    try {
      await revokeOtherSessions()
      await loadSessions()
      setSessionsNotice('Every other session was ended.')
    } catch (err) {
      setSessionsError(err.message)
    } finally {
      setSessionBusy(null)
    }
  }

  // ── #1367: account deletion ───────────────────────────────────────────
  // Two independent guards before the request, plus the server's own
  // re-authentication: the typed phrase (deleteConfirmationMatches, unit
  // tested) gates the button, and window.confirm still runs after it — the
  // same belt-and-suspenders shape as unlinkConnected above, for an action
  // that unlike an unlink cannot be undone by redoing it.
  const hasPassword = accountHasPassword(connectedAccounts)
  const deleteReady
    = deleteConfirmationMatches(deletePhrase)
    && (!hasPassword || deletePassword.length > 0)

  const submitDeleteAccount = async (event) => {
    event.preventDefault()
    if (!deleteReady) return
    if (!window.confirm('Delete your account? This cannot be undone.')) return
    setDeleteBusy(true)
    setDeleteError('')
    try {
      // A password-less account (Google/GitHub only) must NOT send an empty
      // password — Better Auth would answer CREDENTIAL_ACCOUNT_NOT_FOUND
      // instead of falling through to its session-freshness check.
      await deleteAccount(hasPassword ? deletePassword : undefined)
      onDisconnect?.()
      window.location.assign('/')
    } catch (err) {
      setDeleteError(err.message)
      setDeleteBusy(false)
    }
  }

  return (
    <div className="account-page max-w-[760px]">
      <header className="app-page-heading">
        <p className="app-eyebrow">Identity and control</p>
        <h1>Account</h1>
        {/* #1370 item 8: the npm auth library is an internal dependency, not a
            user-facing noun — describe what the account *is*, not what built it. */}
        <p>Your account owns application data. Linked wallets prove on-chain control only.</p>
      </header>

      <section className="card-flat p-5 mb-5">
        <h2 className="serif text-xl mb-3">Profile</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          <dt className="caption">Name</dt><dd>{user?.name || '—'}</dd>
          <dt className="caption">Email</dt><dd>{user?.email || '—'}</dd>
          <dt className="caption">User ID</dt><dd className="mono break-all">{user?.id}</dd>
        </dl>
        {user && (
          <div className="mt-3 flex flex-col gap-2">
            {user.emailVerified === false ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="caption">Email not verified</span>
                <button
                  className="btn-secondary"
                  type="button"
                  disabled={resendDisabled}
                  onClick={sendVerification}
                >
                  {verifyStatus === 'sending' ? 'Sending…' : 'Send verification email'}
                </button>
              </div>
            ) : (
              <span className="caption">Email verified ✓</span>
            )}
            {/* The eternal "requested" line is the PRE-STATUS fallback only.
                Once the GET below has an answer this build recognises, the
                panel is the single source of truth for this click — mounting
                both puts "delivery isn't confirmed…" next to a specific
                suppressed / failed / rate_limited, or a second softer
                "requested" next to an honest sent. Shared predicate so this
                surface and the Generate page cannot drift. */}
            {verifyStatus === 'sent' && shouldShowRequestedFallback(verifyDelivery) && (
              <div className="status" role="status">{VERIFICATION_REQUESTED_MESSAGE}</div>
            )}
            {verifyStatus === 'error' && (
              <div className="status" role="alert">{verifyError}</div>
            )}
            {/* #1748 item 2: the honest state — sent / suppressed /
                rate_limited / failed / unknown — from the auth service's own
                delivery record, replacing the eternal silent 200. Renders
                nothing for a verified account or an unrecognised state. */}
            <VerificationDeliveryStatus status={verifyDelivery} />
          </div>
        )}
      </section>

      <section className="card-flat p-5 mb-5">
        <h2 className="serif text-xl mb-3">Change email</h2>
        <p className="caption mb-3">
          Your address does not change when you submit this. We email a link, and the change happens
          only when the new address opens it.
          {user?.emailVerified
            ? ' Because your current address is verified, the first link goes there — approve it and the second one goes to the new address.'
            : ' The link goes straight to the new address.'}
        </p>
        <form className="flex flex-wrap items-end gap-2" onSubmit={submitEmailChange}>
          <label className="flex flex-col gap-1 grow">
            <span className="caption">New email address</span>
            <input
              type="email"
              required
              autoComplete="email"
              value={newEmail}
              onChange={(event) => setNewEmail(event.target.value)}
            />
          </label>
          <button className="btn-secondary" type="submit" disabled={emailBusy || newEmail.trim() === ''}>
            {emailBusy ? 'Requesting…' : 'Request email change'}
          </button>
        </form>
        <div role="status" aria-live="polite" className={emailNotice ? 'caption mt-3' : 'sr-only'}>
          {emailNotice}
        </div>
        {emailError && <div className="status mt-3" role="alert">{emailError}</div>}
      </section>

      <section className="card-flat p-5 mb-5">
        <h2 className="serif text-xl mb-3">Change password</h2>
        {!connectedLoaded ? (
          <p className="body">Loading…</p>
        ) : !hasPassword ? (
          // Honest rather than a disabled form: /change-password needs a
          // credential row to verify against, and /set-password is
          // server-only in Better Auth (update-user.mjs's
          // createAuthEndpoint.serverOnly), so there is no client path to
          // add one. Saying so beats an input that can only ever fail.
          <p className="body">
            This account signs in with {connectedAccounts.map((account) => connectedProviderLabel(account.providerId)).join(' and ') || 'a linked provider'} only,
            so it has no password to change.
          </p>
        ) : (
          <>
            <p className="caption mb-3">
              Changing your password ends every other signed-in session. This one stays signed in.
            </p>
            <form className="flex flex-col gap-2 max-w-[420px]" onSubmit={submitPasswordChange}>
              <label className="flex flex-col gap-1">
                <span className="caption">Current password</span>
                <input
                  type="password"
                  required
                  autoComplete="current-password"
                  value={passwordForm.current}
                  onChange={(event) => setPasswordForm({ ...passwordForm, current: event.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="caption">New password</span>
                <input
                  type="password"
                  required
                  autoComplete="new-password"
                  minLength={PASSWORD_MIN}
                  value={passwordForm.next}
                  onChange={(event) => setPasswordForm({ ...passwordForm, next: event.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="caption">Confirm new password</span>
                <input
                  type="password"
                  required
                  autoComplete="new-password"
                  minLength={PASSWORD_MIN}
                  value={passwordForm.confirm}
                  onChange={(event) => setPasswordForm({ ...passwordForm, confirm: event.target.value })}
                />
              </label>
              {passwordForm.next !== '' && !passwordRulesMet(passwordForm.next) && (
                <p className="caption">At least {PASSWORD_MIN} characters.</p>
              )}
              {passwordForm.confirm !== '' && !passwordsMatch(passwordForm.next, passwordForm.confirm) && (
                <p className="caption">Passwords do not match.</p>
              )}
              <button className="btn-secondary self-start" type="submit" disabled={passwordBusy || !passwordReady}>
                {passwordBusy ? 'Changing…' : 'Change password'}
              </button>
            </form>
          </>
        )}
        <div role="status" aria-live="polite" className={passwordNotice ? 'caption mt-3' : 'sr-only'}>
          {passwordNotice}
        </div>
        {passwordError && <div className="status mt-3" role="alert">{passwordError}</div>}
      </section>

      <section className="card-flat p-5 mb-5">
        <h2 className="serif text-xl mb-3">Connected accounts</h2>
        <p className="caption mb-3">
          Sign in with any of these — they all reach this one account.
          {providersIntro && ` ${providersIntro}`}
        </p>

        {linkErrorNotice && <div className="status mb-3" role="alert">{linkErrorNotice}</div>}

        {!connectedLoaded ? (
          <p className="body">Loading…</p>
        ) : connectedAccounts.length === 0 ? (
          <p className="body">No connected sign-in methods.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {connectedAccounts.map((account) => {
              const label = connectedProviderLabel(account.providerId)
              const disabled = connectedBusy === account.id || connectedSessionStale || !canUnlink(connectedAccounts.length)
              const title = connectedSessionStale
                ? 'Sign in again to change connected accounts.'
                : !canUnlink(connectedAccounts.length)
                  ? 'This is your only sign-in method — link another before unlinking it.'
                  : undefined
              return (
                <li key={account.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--glass-border)] pt-3">
                  <div className="mono text-sm">{label}</div>
                  <button
                    className="btn-secondary"
                    disabled={disabled}
                    title={title}
                    onClick={() => unlinkConnected(account)}
                  >
                    Unlink
                  </button>
                </li>
              )
            })}
          </ul>
        )}

        {(connectedProviders.google || connectedProviders.github) && (
          <div className="mt-4 flex gap-2">
            {connectedProviders.google && !connectedAccounts.some((a) => a.providerId === 'google') && (
              <button className="btn-secondary" disabled={connectedBusy === 'google' || connectedSessionStale} onClick={() => link('google')}>
                Link Google
              </button>
            )}
            {connectedProviders.github && !connectedAccounts.some((a) => a.providerId === 'github') && (
              <button className="btn-secondary" disabled={connectedBusy === 'github' || connectedSessionStale} onClick={() => link('github')}>
                Link GitHub
              </button>
            )}
          </div>
        )}

        {connectedProvidersError && (
          <p className="status mt-3" role="alert">
            Could not check which sign-in providers are available — Link buttons may be missing above. Reload to try again.
          </p>
        )}

        <div role="status" aria-live="polite" className={connectedNotice ? 'caption mt-3' : 'sr-only'}>
          {connectedNotice}
        </div>
        {connectedError && (
          <div className="status mt-3" role="alert">
            {connectedError}
            {/* SESSION_NOT_FRESH (round-2 review, blocker): the session is
                still signed in, just too old for auth.js's freshness gate
                on /link-social and /unlink-account — re-authenticating is
                the fix, not "you got logged out". Round-4 review finding
                (major): this used to call logout(), which lands on '/' — no
                sign-in form there and no way back to this page. reauthenticate()
                above ends the session the same way, then routes to
                /sign-in?next=/app/account so completing sign-in returns here. */}
            {connectedSessionStale && (
              <button className="btn-secondary ml-2" onClick={reauthenticate}>Sign in again</button>
            )}
          </div>
        )}
      </section>

      <section className="card-flat p-5 mb-5">
        <h2 className="serif text-xl mb-3">Active sessions</h2>
        <p className="caption mb-3">
          Every browser currently signed in as you. Sessions last seven days. Ending one signs that
          browser out immediately.
        </p>

        {!sessionsLoaded ? (
          <p className="body">Loading…</p>
        ) : sessionsStale ? (
          // NOT rendered as "no other sessions" — a stale session can't read
          // the list at all, and an empty list is what a genuinely
          // single-session account looks like. Showing one as the other
          // would be a fabricated all-clear at the exact moment someone is
          // checking whether they have been compromised.
          <p className="body">{SESSIONS_STALE_MESSAGE}</p>
        ) : sessions.length === 0 ? (
          <p className="body">No active sessions listed.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {sessions.map((session) => {
              const current = isCurrentSession(session, currentSession)
              return (
                <li key={session.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--glass-border)] pt-3">
                  <div>
                    <div className="text-sm">
                      {current ? 'This browser' : session.userAgent || 'Unknown browser'}
                      {current && ' · current session'}
                    </div>
                    <div className="caption">
                      Started {formatSessionTimestamp(session.createdAt)}
                      {session.ipAddress ? ` · ${session.ipAddress}` : ''}
                      {' · expires '}{formatSessionTimestamp(session.expiresAt)}
                    </div>
                  </div>
                  {!current && (
                    <button
                      className="btn-secondary"
                      disabled={sessionBusy !== null}
                      onClick={() => revokeOne(session)}
                    >
                      End session
                    </button>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        {/* Deliberately live even in the stale state: /revoke-other-sessions
            is not fresh-gated (session.mjs uses sensitiveSessionMiddleware
            there), so the one control a worried user most needs still
            works when the list itself will not load. */}
        <div className="mt-4">
          <button className="btn-secondary" disabled={sessionBusy !== null} onClick={revokeEveryOther}>
            {sessionBusy === 'others' ? 'Ending…' : 'End all other sessions'}
          </button>
        </div>

        <div role="status" aria-live="polite" className={sessionsNotice ? 'caption mt-3' : 'sr-only'}>
          {sessionsNotice}
        </div>
        {sessionsError && !sessionsStale && <div className="status mt-3" role="alert">{sessionsError}</div>}
      </section>

      <section className="card-flat p-5 mb-5">
        <div className="flex items-center justify-between gap-3 mb-3">
          <div>
            <h2 className="serif text-xl">Linked wallets</h2>
            <p className="caption mt-1">Use Connect Wallet in top bar to add MetaMask, browser, or Circle wallets.</p>
          </div>
        </div>
        {wallets.length === 0 ? (
          <p className="body">No linked wallet. Account-only features remain available.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {wallets.map((wallet) => (
              <li key={wallet.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--glass-border)] pt-3">
                <div>
                  <div className="mono text-sm">{wallet.display_address}</div>
                  <div className="caption">{providerLabel(wallet.provider)} · chain {wallet.chain_id}{wallet.is_primary ? ' · primary' : ''}</div>
                </div>
                <div className="flex gap-2">
                  {!wallet.is_primary && (
                    <button className="btn-secondary" disabled={busy === wallet.id} onClick={() => primary(wallet.id)}>
                      Make primary
                    </button>
                  )}
                  <button className="btn-secondary" disabled={busy === wallet.id} onClick={() => unlink(wallet)}>
                    Unlink
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <div role="status" aria-live="polite" className={notice ? 'caption mt-3' : 'sr-only'}>
          {notice}
        </div>
        {error && <div className="status mt-3" role="alert">{error}</div>}
      </section>

      <section className="card-flat p-5 mb-5">
        <h2 className="serif text-xl mb-3">Delete account</h2>
        <p className="body mb-3">
          Deleting your account cannot be undone, and there is no recovery window. Read what happens
          before you type the confirmation.
        </p>

        {/* Every sentence below is generated from account-deletion.js's
            table→sentence map, which ui/test/account-deletion.test.js pins
            against the ON DELETE actions actually declared in
            backend/archimedes/models/*.py. Hand-written copy here would be a
            claim nothing keeps true. */}
        <p className="caption">Erased outright:</p>
        <ul className="body mb-3 list-disc pl-5">
          {DELETION_ERASED.map((row) => <li key={row.table}>{row.label}</li>)}
        </ul>
        <p className="caption">Kept, with your name taken off them — other accounts can reference these by id, so removing them would break someone else&rsquo;s data:</p>
        <ul className="body mb-3 list-disc pl-5">
          {DELETION_DETACHED.map((row) => <li key={row.table}>{row.label}</li>)}
        </ul>
        <p className="caption">Not removed by this, and you should know it:</p>
        <ul className="body mb-3 list-disc pl-5">
          {DELETION_RETAINED.map((row) => <li key={row.table}>{row.label}</li>)}
        </ul>
        <p className="caption mb-3">
          Anything already published to a blockchain stays there — no deletion here
          can reach it. Server and load-balancer logs age out on their own schedule rather than being
          pulled out per account.
        </p>

        <form className="flex flex-col gap-2 max-w-[420px]" onSubmit={submitDeleteAccount}>
          <label className="flex flex-col gap-1">
            <span className="caption">Type &ldquo;{DELETE_CONFIRMATION_PHRASE}&rdquo; to confirm</span>
            <input
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck="false"
              value={deletePhrase}
              onChange={(event) => setDeletePhrase(event.target.value)}
            />
          </label>
          {hasPassword && (
            <label className="flex flex-col gap-1">
              <span className="caption">Your password</span>
              <input
                type="password"
                autoComplete="current-password"
                value={deletePassword}
                onChange={(event) => setDeletePassword(event.target.value)}
              />
            </label>
          )}
          <button className="btn-secondary self-start" type="submit" disabled={deleteBusy || !deleteReady}>
            {deleteBusy ? 'Deleting…' : 'Delete my account'}
          </button>
        </form>
        {deleteError && <div className="status mt-3" role="alert">{deleteError}</div>}
      </section>

      <button className="btn-secondary" onClick={logout}>Sign out</button>
    </div>
  )
}
