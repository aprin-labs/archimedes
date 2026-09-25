import { useState } from 'react'
import { createPortal } from 'react-dom'
import { getStoredWalletName } from '../config'
import { canStore } from '../storage-consent.js'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const STORAGE_PREFIX = 'archimedes.welcomeProfileSeen.'

const INTEREST_OPTIONS = ['Equities', 'Bonds', 'Commodities', 'Crypto', 'FX']

// WelcomeProfileModal — two modes:
//   `mode="welcome"` (default): first wallet-connect prompt. Has a "Skip"
//     button; on submit OR skip, sets the localStorage gate so it never
//     re-shows for this wallet.
//   `mode="edit"`: invoked from the wallet menu dropdown to view + edit
//     an existing profile. Pre-fills from `existingProfile`. Has Cancel
//     instead of Skip; never touches the localStorage gate.
//
// All fields remain optional in both modes.
export default function WelcomeProfileModal({ walletAddr, onDone, mode = 'welcome', existingProfile = null }) {
  const isEdit = mode === 'edit'
  // Prefill priority: backend profile display_name → wallet name saved at
  // Circle passkey creation (localStorage) → empty. Still fully editable.
  const [displayName, setDisplayName] = useState(
    existingProfile?.display_name || (walletAddr && getStoredWalletName(walletAddr)) || ''
  )
  const [email, setEmail] = useState(existingProfile?.email || '')
  const [selectedInterests, setSelectedInterests] = useState(existingProfile?.interests || [])
  const [attribution, setAttribution] = useState(existingProfile?.attribution || '')
  const [marketingOptIn, setMarketingOptIn] = useState(existingProfile?.marketing_opt_in || false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const toggleInterest = (interest) => {
    setSelectedInterests(prev =>
      prev.includes(interest)
        ? prev.filter(i => i !== interest)
        : [...prev, interest]
    )
  }

  const markSeen = () => {
    // Edit mode never touches the gate — only the welcome flow sets it.
    if (isEdit) return
    if (walletAddr) {
      // Functional category (#1647): with functional storage rejected the
      // "already asked this wallet" marker is not kept, so the skippable
      // welcome prompt can appear again — the stated fallback.
      const key = STORAGE_PREFIX + walletAddr.toLowerCase()
      if (canStore(key)) localStorage.setItem(key, '1')
    }
  }

  const handleCancel = () => {
    // Welcome mode: cancel = skip, sets the gate.
    // Edit mode: cancel = dismiss, no gate change.
    markSeen()
    onDone?.()
  }

  const handleSubmit = async () => {
    setError('')
    setSubmitting(true)
    try {
      const payload = {
        wallet_address: walletAddr,
        display_name: displayName.trim() || null,
        email: email.trim() || null,
        interests: selectedInterests.length > 0 ? selectedInterests : null,
        attribution: attribution.trim() || null,
        marketing_opt_in: marketingOptIn,
      }
      const res = await fetch(`${API_BASE}/api/user/profile`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',  // Better Auth account + matching linked wallet required
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(text || `Profile save failed (${res.status})`)
      }
      markSeen()
      onDone?.(await res.json())
    } catch (e) {
      setError(e.message || 'Failed to save profile')
    } finally {
      setSubmitting(false)
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 flex items-center justify-center z-[1000]"
      style={{ background: 'rgba(0,0,0,0.78)', backdropFilter: 'blur(6px)' }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="welcome-modal-title"
    >
      <div
        className="card-elevated p-8 max-w-[560px] w-[92vw]"
        style={{
          background: 'var(--surface-1)',
          maxHeight: '90vh',
          overflowY: 'auto',
          boxShadow: '0 24px 60px rgba(0,0,0,0.55)',
        }}
      >
        <div className="caption mb-3 uppercase tracking-wider text-[var(--text-3)]" style={{ fontSize: '0.78rem' }}>
          {isEdit ? 'Your Profile' : 'Welcome to Archimedes'}
        </div>
        <h3 id="welcome-modal-title" className="font-serif mb-2" style={{ fontSize: '1.85rem', lineHeight: 1.2, letterSpacing: '-0.02em' }}>
          {isEdit ? 'Edit your profile' : 'Personalize your experience'}
        </h3>
        <p className="mb-5 leading-relaxed" style={{ fontSize: '0.98rem', color: 'var(--text-2)' }}>
          {isEdit
            ? 'Update what we show alongside your wallet. All fields remain optional; leave any blank to clear it.'
            : 'All fields are optional. Your account is your identity; a verified linked wallet provides on-chain attribution. You can skip this entirely.'}
        </p>

        <div className="grid grid-cols-1 gap-4">
          <label className="block">
            <span className="block mb-1.5" style={{ fontSize: '0.9rem', color: 'var(--text-2)', fontWeight: 500 }}>Display name</span>
            <input
              type="text"
              value={displayName}
              onChange={e => setDisplayName(e.target.value)}
              placeholder="Alice"
              maxLength={128}
              className="chat-input w-full"
              style={{ padding: '12px 14px', fontSize: '1rem' }}
              disabled={submitting}
            />
          </label>

          <label className="block">
            <span className="block mb-1.5" style={{ fontSize: '0.9rem', color: 'var(--text-2)', fontWeight: 500 }}>
              Email <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>(optional)</span>
            </span>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@example.com"
              maxLength={256}
              className="chat-input w-full"
              style={{ padding: '12px 14px', fontSize: '1rem' }}
              disabled={submitting}
            />
          </label>

          <div>
            <span className="block mb-2" style={{ fontSize: '0.9rem', color: 'var(--text-2)', fontWeight: 500 }}>Interests</span>
            <div className="flex flex-wrap gap-2">
              {INTEREST_OPTIONS.map(interest => (
                <button
                  key={interest}
                  type="button"
                  className={`tag ${selectedInterests.includes(interest) ? 'tag-positive' : 'tag-muted'}`}
                  onClick={() => toggleInterest(interest)}
                  disabled={submitting}
                  style={{ cursor: 'pointer', fontSize: '0.88rem', padding: '6px 12px' }}
                >
                  {interest}
                </button>
              ))}
            </div>
          </div>

          <label className="block">
            <span className="block mb-1.5" style={{ fontSize: '0.9rem', color: 'var(--text-2)', fontWeight: 500 }}>
              Attribution <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>(optional)</span>
            </span>
            <input
              type="text"
              value={attribution}
              onChange={e => setAttribution(e.target.value)}
              placeholder="How did you hear about us?"
              maxLength={256}
              className="chat-input w-full"
              style={{ padding: '12px 14px', fontSize: '1rem' }}
              disabled={submitting}
            />
          </label>

          <label className="flex items-center gap-2.5 cursor-pointer">
            <input
              type="checkbox"
              checked={marketingOptIn}
              onChange={e => setMarketingOptIn(e.target.checked)}
              disabled={submitting}
              style={{ width: 18, height: 18, cursor: 'pointer' }}
            />
            <span style={{ fontSize: '0.95rem', color: 'var(--text-2)' }}>Keep me updated on new strategies and features</span>
          </label>
        </div>

        {error && <div className="info-box warning mt-3">{error}</div>}

        <div className="flex justify-between gap-3 mt-5">
          <button
            className="btn btn-outline"
            onClick={handleCancel}
            disabled={submitting}
          >
            {isEdit ? 'Cancel' : 'Skip for now'}
          </button>
          <button
            className="btn btn-primary"
            onClick={handleSubmit}
            disabled={submitting}
          >
            {submitting ? 'Saving…' : (isEdit ? 'Save Changes' : 'Save Profile')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
