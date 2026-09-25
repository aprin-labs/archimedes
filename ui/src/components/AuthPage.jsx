import { useEffect, useState } from 'react'

import { useAuth } from '../AuthContext'
import { oauthErrorMessage } from '../auth-errors'
import {
  requestPasswordReset,
  resendVerificationEmail,
  resetPassword,
  signInEmail,
  signInSocial,
  signUpEmail,
} from '../auth-client'
import {
  PASSWORD_MAX,
  PASSWORD_MIN,
  passwordRules,
  passwordRulesMet,
  passwordsMatch,
  passwordStrength,
} from '../password-rules'
import { postAuthPath } from '../routes'
import BrandMark from './BrandMark'

const STRENGTH_COLORS = ['var(--text-4)', 'var(--text-4)', 'var(--warning, #b08a3e)', 'var(--accent)', 'var(--accent)']

// Better Auth returns this identical response whether or not the address has
// an account (auth/auth.js sendResetPassword sits behind that check) — the UI
// shows the same copy in both cases and must never branch on the result.
const RESET_REQUESTED_MESSAGE = 'If that email has an Archimedes account, a reset link is on its way.'
const VERIFICATION_RESENT_MESSAGE = 'Verification email sent — check your inbox.'

// The three statements under "Account before wallet" are load-bearing product
// claims, so each one is checkable against the live path (repo rule #1):
//   1. Email and password work without a wallet.
//      auth-client.js signUpEmail/signInEmail POST name/email/password only —
//      no address, no signature, no chain id anywhere in the request.
//   2. Wallet linking requires signature proof.
//      linked-wallets.js linkConnectedWallet always signs the server's SIWE
//      challenge, and wallet_routes.verify_wallet_challenge 401s with
//      "Invalid wallet signature" when the recovered signer does not match.
//   3. Generation settles real testnet USDC on Arc public testnet.
//      GET /api/generate/quote on prod answers dry_run: false,
//      price "$2.000000" USDC, chain arcTestnet — generate is paid,
//      not free. chain-config.js DEFAULT_CHAIN_ID 5042002 is Arc's
//      public testnet (https://rpc.testnet.arc.network).
// The sentence that used to sit above them was removed rather than reworded:
// it scoped a linked wallet to chain activity alone, which the code does not
// bear out — user_routes._extract_linked_wallet gates PII profile reads on
// one, and wallet_routes.claim_legacy_wallet_data reclaims pre-account data
// with one. Neither touches the chain. ui/test/auth-page-copy.test.js keeps
// that wording out; deliberately not quoted here, so the guard cannot be
// tripped (or lulled) by a comment.
const ACCOUNT_BOUNDARY_PROOFS = [
  'Email and password work without a wallet.',
  'Wallet linking requires signature proof.',
  'Generation settles real testnet USDC on Arc public testnet.',
]

/* Google's four-colour "G", reproduced unmodified per Google's brand
   guidelines: the official artwork, no recolouring, and a fixed square box
   locked to the intrinsic 48×48 viewBox so it can never be stretched. It is
   decorative — the button's own text names the provider — hence aria-hidden.
   Clear space and the 44px minimum target come from .auth-social in App.css. */
function GoogleMark() {
  return (
    <svg
      className="auth-social__icon"
      width="18"
      height="18"
      viewBox="0 0 48 48"
      aria-hidden="true"
      focusable="false"
    >
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.28-3.14.76-4.59l-7.97-6.19C.92 16.46 0 20.12 0 24s.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  )
}

/* GitHub's Invertocat, the `mark-github` Octicon. Drawn in currentColor so it
   inherits --text-1: solid near-black on the light theme, solid near-white on
   dark — the two monochrome treatments GitHub's logo guidelines permit. The
   16×16 viewBox with equal width/height keeps the aspect ratio intact. */
function GitHubMark() {
  return (
    <svg
      className="auth-social__icon"
      width="18"
      height="18"
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.65 7.65 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
    </svg>
  )
}

/** Live checklist + guidance strength meter, shared by sign-up and reset-password. */
function PasswordChecklist({ password, confirm, showMismatch }) {
  const rules = passwordRules(password)
  const match = passwordsMatch(password, confirm)
  const strength = passwordStrength(password)
  return (
    <div aria-live="polite" className="flex flex-col gap-1.5">
      <div className="flex gap-1" aria-hidden="true">
        {[1, 2, 3, 4].map((step) => (
          <span
            key={step}
            className="h-1 flex-1 rounded"
            style={{ background: step <= strength.score ? STRENGTH_COLORS[strength.score] : 'var(--glass-border)' }}
          />
        ))}
      </div>
      {password.length > 0 && (
        <span className="caption">
          Strength: {strength.label} — guidance only; the length rule below is the requirement.
        </span>
      )}
      <ul className="caption flex flex-col gap-0.5" id="password-rules" aria-label="Password requirements">
        {rules.map((rule) => (
          <li key={rule.id} className={rule.met ? 'text-[var(--accent)]' : undefined}>
            {rule.met ? '✓' : '○'} {rule.label}
          </li>
        ))}
        <li className={match ? 'text-[var(--accent)]' : undefined}>
          {match ? '✓' : '○'} Passwords match
        </li>
      </ul>
      {showMismatch && (
        <div className="status" role="alert" id="confirm-mismatch">
          Passwords do not match.
        </div>
      )}
    </div>
  )
}

function ForgotPasswordForm({ email, onBack, callbackOrigin }) {
  const [address, setAddress] = useState(email)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [sent, setSent] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await requestPasswordReset(address, `${callbackOrigin}/reset-password`)
      setSent(true)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (sent) {
    return (
      <div className="flex flex-col gap-4">
        <p className="body">{RESET_REQUESTED_MESSAGE}</p>
        <button className="btn-secondary" type="button" onClick={onBack}>Back to sign in</button>
      </div>
    )
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <label className="flex flex-col gap-1.5">
        <span className="caption">Email</span>
        <input
          required
          type="email"
          autoComplete="email"
          value={address}
          onChange={(event) => setAddress(event.target.value)}
        />
      </label>
      {error && <div className="status" role="alert">{error}</div>}
      <button className="btn-primary" type="submit" disabled={busy}>
        {busy ? 'Working…' : 'Send reset link'}
      </button>
      <button className="btn-secondary" type="button" onClick={onBack} disabled={busy}>Back to sign in</button>
    </form>
  )
}

function ResetPasswordForm({ token, callbackOrigin }) {
  const [form, setForm] = useState({ password: '', confirm: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const rulesMet = passwordRulesMet(form.password)
  const match = passwordsMatch(form.password, form.confirm)
  const showMismatch = form.confirm.length > 0 && !match

  if (!token) {
    return (
      <div className="flex flex-col gap-4">
        <p className="body">This reset link is invalid or has expired.</p>
        <a className="btn-primary text-center" href={`${callbackOrigin}/sign-in`}>Request a new one</a>
      </div>
    )
  }

  if (done) {
    return (
      <div className="flex flex-col gap-4">
        <p className="body">Password updated. Every prior session was signed out.</p>
        <a className="btn-primary text-center" href={`${callbackOrigin}/sign-in`}>Sign in</a>
      </div>
    )
  }

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await resetPassword(form.password, token)
      setDone(true)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <label className="flex flex-col gap-1.5">
        <span className="caption">New password</span>
        <input
          required
          type="password"
          minLength={PASSWORD_MIN}
          maxLength={PASSWORD_MAX}
          autoComplete="new-password"
          aria-describedby="password-rules"
          value={form.password}
          onChange={(event) => setForm({ ...form, password: event.target.value })}
        />
      </label>
      <label className="flex flex-col gap-1.5">
        <span className="caption">Confirm new password</span>
        <input
          required
          type="password"
          maxLength={PASSWORD_MAX}
          autoComplete="new-password"
          aria-invalid={showMismatch}
          aria-describedby={showMismatch ? 'password-rules confirm-mismatch' : 'password-rules'}
          value={form.confirm}
          onChange={(event) => setForm({ ...form, confirm: event.target.value })}
        />
      </label>
      <PasswordChecklist password={form.password} confirm={form.confirm} showMismatch={showMismatch} />
      {error && <div className="status" role="alert">{error}</div>}
      <button className="btn-primary" type="submit" disabled={busy || !rulesMet || !match}>
        {busy ? 'Working…' : 'Set new password'}
      </button>
    </form>
  )
}

export default function AuthPage({ mode, oauthError }) {
  const creating = mode === 'sign-up'
  const resetting = mode === 'reset-password'
  const next = postAuthPath(window.location.search)
  const callbackURL = `${window.location.origin}${next}`
  const callbackOrigin = window.location.origin
  const resetToken = resetting ? new URLSearchParams(window.location.search).get('token') : null
  // Surfaces the account-linking rejection (routes.js bounces `/?error=...`
  // here) or any other OAuth callback error — never shown on sign-up or
  // reset-password, since those screens can't be the redirect's origin. Named
  // `oauthError` (not `error`) to avoid shadowing the submit-flow `error`
  // state declared below.
  const oauthNotice = !creating && !resetting ? oauthErrorMessage(oauthError) : null
  const { user, refresh } = useAuth()
  const [form, setForm] = useState({ name: '', email: '', password: '', confirm: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [needsVerification, setNeedsVerification] = useState(false)
  const [screen, setScreen] = useState('credentials') // 'credentials' | 'forgot'

  // Sign-up validity mirrors the SERVER's rules exactly (length only — see
  // password-rules.js). The strength meter (PasswordChecklist) is guidance
  // and never gates.
  const rulesMet = passwordRulesMet(form.password)
  const match = passwordsMatch(form.password, form.confirm)
  const showMismatch = creating && form.confirm.length > 0 && !match
  const canSubmit = !creating || (rulesMet && match)

  useEffect(() => {
    if (user && !resetting) window.location.replace(next)
  }, [user, next, resetting])

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    setNeedsVerification(false)
    try {
      if (creating) {
        await signUpEmail(form.name, form.email, form.password, callbackURL)
        await signInEmail(form.email, form.password, callbackURL)
      } else {
        await signInEmail(form.email, form.password, callbackURL)
      }
      await refresh()
      window.location.assign(next)
    } catch (err) {
      setError(err.message)
      // Better Auth answers an unverified sign-in with 403 (BASE_ERROR_CODES
      // .EMAIL_NOT_VERIFIED) — give that specific lockout an escape hatch
      // instead of leaving the user stuck on a bare error string.
      if (err.status === 403) setNeedsVerification(true)
    } finally {
      setBusy(false)
    }
  }

  const resendVerification = async () => {
    setBusy(true)
    setError('')
    try {
      await resendVerificationEmail(form.email, callbackURL)
      setNotice(VERIFICATION_RESENT_MESSAGE)
      setNeedsVerification(false)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const social = async (provider) => {
    setBusy(true)
    setError('')
    try {
      await signInSocial(provider, callbackURL)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  const title = resetting ? 'Reset your password' : creating ? 'Create your account' : screen === 'forgot' ? 'Reset your password' : 'Sign in'

  return (
    <main className="auth-site">
      <div className="auth-shell">
        <section className="auth-context" aria-label="Archimedes account boundary">
          <a href="/" className="auth-brand" aria-label="Archimedes home">
            <BrandMark />
            <span>Archimedes</span>
          </a>
          <div>
            <p className="public-kicker">Account before wallet</p>
            <h2>Research stays linked to you.</h2>
          </div>
          <ul className="auth-proof-list">
            {ACCOUNT_BOUNDARY_PROOFS.map((proof) => (
              <li key={proof}>{proof}</li>
            ))}
          </ul>
        </section>

        <section className="auth-form-panel" aria-labelledby="auth-title">
        <a href="/" className="caption text-[var(--accent)]">← Archimedes</a>
        <h1 id="auth-title" className="serif text-[2rem] mt-4 mb-2">{title}</h1>

        {resetting ? (
          <>
            <p className="body mb-6">Choose a new password for your Archimedes account.</p>
            <ResetPasswordForm token={resetToken} callbackOrigin={callbackOrigin} />
          </>
        ) : screen === 'forgot' ? (
          <>
            <p className="body mb-6">Enter your account email and we&rsquo;ll send a reset link.</p>
            <ForgotPasswordForm
              email={form.email}
              callbackOrigin={callbackOrigin}
              onBack={() => setScreen('credentials')}
            />
          </>
        ) : (
          <>
            <p className="body mb-6">
              Account owns your strategies and settings. Wallets stay optional and link only after signature proof.
            </p>

            {oauthNotice && (
              <div className="status mb-4" role="alert" id="oauth-error">
                {oauthNotice}
              </div>
            )}

            <form onSubmit={submit} className="flex flex-col gap-4">
              {creating && (
                <label className="flex flex-col gap-1.5">
                  <span className="caption">Name</span>
                  <input
                    required
                    autoComplete="name"
                    value={form.name}
                    onChange={(event) => setForm({ ...form, name: event.target.value })}
                  />
                </label>
              )}
              <label className="flex flex-col gap-1.5">
                <span className="caption">Email</span>
                <input
                  required
                  type="email"
                  autoComplete="email"
                  value={form.email}
                  onChange={(event) => setForm({ ...form, email: event.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="caption">Password</span>
                <input
                  required
                  type="password"
                  minLength={PASSWORD_MIN}
                  maxLength={PASSWORD_MAX}
                  autoComplete={creating ? 'new-password' : 'current-password'}
                  aria-describedby={creating ? 'password-rules' : undefined}
                  value={form.password}
                  onChange={(event) => setForm({ ...form, password: event.target.value })}
                />
              </label>
              {!creating && (
                <div className="auth-forgot-row">
                  {/* Was `caption text-[var(--accent)] text-left`: an unstyled
                      <button> stretched by the form's flex column, with no
                      affordance beyond colour and no focus ring of its own
                      (the auth screen has no :focus-visible rule — see the
                      note in App.css near the input block). .auth-quiet-link
                      is the design-system treatment: accent text, an offset
                      underline, a 44px target, and an explicit focus ring. */}
                  <button
                    type="button"
                    className="auth-quiet-link"
                    onClick={() => { setScreen('forgot'); setError(''); setNotice(''); setNeedsVerification(false) }}
                  >
                    Forgot password?
                  </button>
                </div>
              )}
              {creating && (
                <>
                  <label className="flex flex-col gap-1.5">
                    <span className="caption">Confirm password</span>
                    {/* aria-invalid alone said "invalid entry" with no statement of
                        what was wrong: the mismatch alert fires once while the user
                        is still typing and is unreachable afterwards, and the rules
                        list was never associated with the field (3.3.1). */}
                    <input
                      required
                      type="password"
                      maxLength={PASSWORD_MAX}
                      autoComplete="new-password"
                      aria-invalid={showMismatch}
                      aria-describedby={showMismatch ? 'password-rules confirm-mismatch' : 'password-rules'}
                      value={form.confirm}
                      onChange={(event) => setForm({ ...form, confirm: event.target.value })}
                    />
                  </label>
                  {/* Unmet rules inherit .caption's --text-3 rather than --text-4:
                      on the base palette --text-4 is #3f3f46, i.e. 1.73:1 on the
                      card — the rule text a user needs precisely when their
                      password was rejected was the least readable text here.
                      The ✓/○ glyph stays the state signal (1.4.1). */}
                  <PasswordChecklist password={form.password} confirm={form.confirm} showMismatch={showMismatch} />
                </>
              )}
              {notice && <div className="status" role="status">{notice}</div>}
              {error && (
                <div className="status" role="alert">
                  {error}
                  {needsVerification && (
                    <>
                      {' '}
                      <button type="button" className="text-[var(--accent)]" onClick={resendVerification} disabled={busy}>
                        Resend verification email
                      </button>
                    </>
                  )}
                </div>
              )}
              <button
                className="btn-primary"
                type="submit"
                disabled={busy || !canSubmit}
                aria-describedby={creating && !canSubmit ? 'password-rules' : undefined}
              >
                {busy ? 'Working…' : creating ? 'Create account' : 'Sign in'}
              </button>
            </form>

            {/* Directly under the submit button, not buried below the OAuth
                block: the cross-link is the second thing a wrong-screen
                visitor needs, so it gets its own bordered row and a link with
                a real affordance rather than a caption-sized aside. */}
            <p className="auth-alt-action">
              <span>{creating ? 'Already registered?' : 'New to Archimedes?'}</span>
              <a
                className="auth-alt-action__link"
                href={`${creating ? '/sign-in' : '/sign-up'}?next=${encodeURIComponent(next)}`}
              >
                {creating ? 'Sign in' : 'Create account'}
                <span aria-hidden="true"> →</span>
              </a>
            </p>

            {/* Hairline only, no "or continue with" label: each button already
                says "Continue with …", and a label there would read as an
                alternative to the create-account row directly above it. */}
            <div className="auth-social-group">
              <button type="button" className="btn-secondary auth-social" onClick={() => social('google')} disabled={busy}>
                <GoogleMark />
                <span>Continue with Google</span>
              </button>
              <button type="button" className="btn-secondary auth-social" onClick={() => social('github')} disabled={busy}>
                <GitHubMark />
                <span>Continue with GitHub</span>
              </button>
            </div>

            <p className="caption mt-5">
              Circle wallet passkeys authorize Circle smart wallets. They do not sign in to Archimedes.
            </p>
          </>
        )}
        </section>
      </div>
    </main>
  )
}
