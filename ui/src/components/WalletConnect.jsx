import { Fragment, useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import {
  connectWallet,
  getAvailableProviders,
  getConnectedProvider,
  getUsdcBalance,
  CIRCLE_PROVIDER_ID,
} from '../config'
import { sanitizeWalletName } from '../circle-wallet'
import { linkConnectedWallet } from '../linked-wallets'
import useDialogFocus from '../hooks/useDialogFocus'

export default function WalletConnect({ address, displayName, onConnect, onDisconnect, onEditProfile }) {
  const [showModal, setShowModal] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const [discoveryTick, setDiscoveryTick] = useState(0)
  const [copied, setCopied] = useState(false)
  const [balance, setBalance] = useState(null)
  const [walletName, setWalletName] = useState('')
  const menuRef = useRef(null)
  const modalRef = useDialogFocus(showModal)
  const providerId = getConnectedProvider()
  const isPasskey = providerId === CIRCLE_PROVIDER_ID

  // EIP-6963 wallets announce themselves asynchronously. The set can grow
  // after first render — re-derive `available` when a new wallet announces.
  useEffect(() => {
    const bump = () => setDiscoveryTick(t => t + 1)
    window.addEventListener('eip6963:announceProvider', bump)
    // Re-prompt wallets to announce when the modal opens, in case any
    // loaded after our initial requestProvider in config.js.
    if (showModal) window.dispatchEvent(new Event('eip6963:requestProvider'))
    return () => window.removeEventListener('eip6963:announceProvider', bump)
  }, [showModal])

  // discoveryTick is read here only to force re-computation of `available`
  // when the EIP-6963 listener bumps it; the value itself is unused.
  void discoveryTick
  const available = getAvailableProviders()

  // Allow other components (e.g. WalletGate on logged-out pages) to open
  // this modal without prop-drilling. Dispatch `open-wallet-modal` to
  // trigger; WalletGate's "Connect Wallet" button uses this.
  useEffect(() => {
    const open = () => setShowModal(true)
    window.addEventListener('open-wallet-modal', open)
    return () => window.removeEventListener('open-wallet-modal', open)
  }, [])

  // Esc closes the wallet-connect modal (Issue #338 item 1)
  useEffect(() => {
    if (!showModal) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setShowModal(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [showModal])

  // Close the connected-wallet dropdown on outside click or Escape.
  useEffect(() => {
    if (!menuOpen) return undefined
    const onDocClick = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  // Fetch the USDC balance on connect + whenever the dropdown opens, so the
  // number is fresh after the user faucets or deposits without us subscribing
  // to chain events. Returns null on failure (caller renders placeholder).
  useEffect(() => {
    if (!address) { setBalance(null); return undefined }
    let cancelled = false
    getUsdcBalance(address).then(b => { if (!cancelled) setBalance(b) })
    return () => { cancelled = true }
  }, [address, menuOpen])

  const handleCopy = async () => {
    if (!address) return
    try {
      await navigator.clipboard.writeText(address)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Older browsers / blocked clipboard — silently fail; user can
      // long-press the address to copy via native selection.
    }
  }

  const handleConnect = async (providerId, opts) => {
    setBusy(true)
    setError('')
    try {
      const result = await connectWallet(providerId, opts)

      // Connection alone grants nothing. Persist link only after account-bound,
      // short-lived SIWE proof succeeds.
      await linkConnectedWallet(result)

      setShowModal(false)
      onConnect(result.address)
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  if (address) {
    const shortAddr = `${address.slice(0, 6)}…${address.slice(-4)}`
    return (
      <div className="wallet-menu" ref={menuRef} style={{ position: 'relative' }}>
        <button
          type="button"
          className="wallet-chip"
          onClick={() => setMenuOpen(v => !v)}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          title={displayName ? `${displayName} · ${shortAddr}` : shortAddr}
        >
          <span className="dot" />
          <span>{displayName || shortAddr}</span>
          <span
            className={`i-lucide-chevron-${menuOpen ? 'up' : 'down'}`}
            style={{ width: 14, height: 14, marginLeft: 4 }}
            aria-hidden="true"
          />
        </button>
        {menuOpen && (
          <div
            className="wallet-dropdown"
            role="menu"
            style={{
              position: 'absolute',
              right: 0,
              top: 'calc(100% + 6px)',
              minWidth: 220,
              background: 'var(--surface-1)',
              border: '1px solid var(--glass-border)',
              borderRadius: 10,
              boxShadow: '0 10px 30px rgba(0,0,0,0.45)',
              padding: 6,
              zIndex: 50,
            }}
          >
            <div
              className="caption"
              style={{ padding: '8px 10px', color: 'var(--text-4)', borderBottom: '1px solid var(--glass-border)', marginBottom: 4 }}
            >
              <div style={{ color: 'var(--text-2)', fontWeight: 600, fontSize: '0.85rem', marginBottom: 2 }}>
                {displayName || 'Wallet'}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span className="mono" style={{ fontSize: '0.72rem' }} title={address}>{shortAddr}</span>
                <button
                  type="button"
                  onClick={handleCopy}
                  title={copied ? 'Copied!' : 'Copy full address'}
                  aria-label={copied ? 'Copied' : 'Copy full address'}
                  style={{
                    background: 'transparent', border: 'none', color: copied ? 'var(--accent)' : 'var(--text-3)',
                    cursor: 'pointer', padding: 2, display: 'inline-flex', alignItems: 'center',
                  }}
                >
                  <span
                    className={copied ? 'i-lucide-check' : 'i-lucide-copy'}
                    style={{ width: 12, height: 12 }}
                    aria-hidden="true"
                  />
                </button>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 6, fontSize: '0.74rem' }}>
                <span className="i-lucide-wallet" style={{ width: 12, height: 12, color: 'var(--text-3)' }} aria-hidden="true" />
                <span style={{ color: 'var(--text-2)' }}>
                  {balance === null
                    ? <span style={{ color: 'var(--text-4)' }}>Loading USDC…</span>
                    : <><strong style={{ color: 'var(--text-1)' }}>{balance.toFixed(2)}</strong> USDC</>}
                </span>
                {balance !== null && balance < 1 && (
                  <a
                    href="https://faucet.circle.com/"
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      marginLeft: 'auto', fontSize: '0.7rem', color: 'var(--accent)',
                      textDecoration: 'none',
                    }}
                    title="Get test USDC from Circle's faucet (20 USDC / 2h on Arc)"
                  >
                    Faucet →
                  </a>
                )}
              </div>
              {isPasskey && (
                <div
                  style={{
                    display: 'flex', alignItems: 'center', gap: 4, marginTop: 6,
                    padding: '4px 6px', background: 'rgba(99, 102, 241, 0.08)',
                    border: '1px solid rgba(99, 102, 241, 0.18)', borderRadius: 6,
                    fontSize: '0.68rem', color: 'var(--text-3)',
                  }}
                  title="Smart-contract wallet on Arc — backed by a passkey on this device. No seed phrase. No browser extension."
                >
                  <span className="i-lucide-fingerprint" style={{ width: 11, height: 11 }} aria-hidden="true" />
                  <span>Circle Modular Wallet</span>
                </div>
              )}
            </div>
            <button
              type="button"
              role="menuitem"
              className="wallet-menu-item"
              onClick={() => { setMenuOpen(false); onEditProfile?.() }}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                width: '100%', padding: '8px 10px', textAlign: 'left',
                background: 'transparent', border: 'none', color: 'var(--text-1)',
                fontSize: '0.85rem', cursor: 'pointer', borderRadius: 6,
              }}
            >
              <span className="i-lucide-user-pen" style={{ width: 16, height: 16 }} />
              <span>Profile</span>
            </button>
            {/* Faucet — always visible in the dropdown so the user can top up
                regardless of current balance. Opens in a new tab so the
                Archimedes session isn't unloaded. The conditional inline link
                above (balance < 1) is kept as the urgent-state nudge; this is
                the durable entry point. */}
            <a
              role="menuitem"
              href="https://faucet.circle.com/"
              target="_blank"
              rel="noreferrer"
              onClick={() => setMenuOpen(false)}
              title="Get test USDC from Circle's faucet (20 USDC every 2h on Arc testnet)"
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                width: '100%', padding: '8px 10px', textAlign: 'left',
                background: 'transparent', border: 'none', color: 'var(--text-1)',
                fontSize: '0.85rem', cursor: 'pointer', borderRadius: 6,
                textDecoration: 'none',
              }}
            >
              <span className="i-lucide-droplets" style={{ width: 16, height: 16 }} />
              <span style={{ flex: 1 }}>Faucet</span>
              <span
                className="i-lucide-external-link"
                style={{ width: 12, height: 12, color: 'var(--text-4)' }}
                aria-hidden="true"
              />
            </a>
            <button
              type="button"
              role="menuitem"
              className="wallet-menu-item"
              onClick={() => { setMenuOpen(false); onDisconnect?.() }}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                width: '100%', padding: '8px 10px', textAlign: 'left',
                background: 'transparent', border: 'none', color: 'var(--text-1)',
                fontSize: '0.85rem', cursor: 'pointer', borderRadius: 6,
              }}
            >
              <span className="i-lucide-log-out" style={{ width: 16, height: 16 }} />
              <span>Disconnect</span>
            </button>
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <button type="button" className="wallet-chip" onClick={() => setShowModal(true)}>
        Connect Wallet
      </button>

      {/* The entry point to every on-chain action. It was portalled to <body>
          with no dialog role, no accessible name and no focus move, so after
          clicking "Connect Wallet" a screen-reader user's focus stayed on the
          trigger behind the overlay and reaching the provider buttons meant
          tabbing through the whole page behind it (2.4.3 / 4.1.2). Escape was
          already handled above; the trap and focus restore come from the
          shared hook. */}
      {showModal && createPortal(
        <div className="modal-overlay" onClick={() => setShowModal(false)}>
          <div
            ref={modalRef}
            tabIndex={-1}
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="wallet-modal-title"
            onClick={e => e.stopPropagation()}
          >
            <h3 id="wallet-modal-title">Connect Wallet</h3>
            <p style={{ fontSize: '0.98rem', color: 'var(--text-2)', lineHeight: 1.55, marginBottom: 14 }}>
              {available.length > 1
                ? 'Connect a Circle passkey wallet, or connect a browser wallet to interact with Arc Testnet contracts.'
                : 'Connect a Circle passkey wallet to interact with Arc Testnet contracts. No browser extension needed.'
              }
            </p>

            <div
              style={{
                marginTop: 8,
                marginBottom: 16,
                padding: '14px 16px',
                background: 'var(--surface-2)',
                border: '1px solid var(--glass-border)',
                borderRadius: 10,
                lineHeight: 1.6,
                fontSize: '0.88rem',
                color: 'var(--text-2)',
              }}
            >
              Archimedes only reads your wallet address — it never custodies your USDC.
              Deposits live in your non-custodial vault contract; the agent has rebalance
              authority only, not withdraw-to-platform. Source open at{' '}
              <a
                href="https://github.com/aprin-labs/archimedes"
                target="_blank"
                rel="noreferrer"
              >
                github.com/aprin-labs/archimedes
              </a>
              . Testnet only — fake USDC, no value at risk.
            </div>

            {available.length === 0 ? (
              <div className="info-box warning mt-3">
                No wallets detected. Install <a href="https://metamask.io" target="_blank" rel="noreferrer">MetaMask</a> or{' '}
                <a href="https://www.coinbase.com/wallet" target="_blank" rel="noreferrer">Coinbase Wallet</a>.
              </div>
            ) : (
              <div className="wallet-options">
                {available.map((p, i) => {
                  // Insert a divider + label between the passkey option (if
                  // present, always first) and the EOA wallets that follow.
                  // Visually separates "no extension needed" from "browser
                  // extension required".
                  const prev = available[i - 1]
                  const showDivider = prev?.id === CIRCLE_PROVIDER_ID && p.id !== CIRCLE_PROVIDER_ID
                  return (
                    <Fragment key={p.id}>
                      {showDivider && (
                        <div
                          className="caption"
                          style={{
                            display: 'flex', alignItems: 'center', gap: 8,
                            margin: '6px 0', color: 'var(--text-3)',
                            fontSize: '0.7rem', textTransform: 'uppercase',
                            letterSpacing: '0.05em',
                          }}
                        >
                          <span style={{ flex: 1, height: 1, background: 'var(--glass-border)' }} />
                          <span>or use a browser wallet</span>
                          <span style={{ flex: 1, height: 1, background: 'var(--glass-border)' }} />
                        </div>
                      )}
                      {p.id === CIRCLE_PROVIDER_ID ? (
                        // Circle Modular Wallets — lead with the Circle ecosystem,
                        // then two EXPLICIT choices so users can pick a specific
                        // existing wallet (discoverable passkey picker) instead of
                        // always minting a new one.
                        <div
                          style={{
                            border: '1px solid var(--glass-border)',
                            borderRadius: 10,
                            padding: 12,
                            background: 'var(--surface-2)',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 10,
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                            {p.iconDataUri ? (
                              <img src={p.iconDataUri} alt="" width={20} height={20} style={{ borderRadius: 4 }} />
                            ) : (
                              <span className={`wallet-icon ${p.icon} w-5 h-5`} />
                            )}
                            <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                              <span style={{ fontWeight: 600, color: 'var(--text-1)' }}>Circle Modular Wallets</span>
                              <span style={{ fontSize: '0.78rem', color: 'var(--text-3)', lineHeight: 1.4 }}>
                                Face ID / Touch ID · smart-contract account on Arc · no extension, no seed phrase
                              </span>
                            </span>
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            <input
                              type="text"
                              value={walletName}
                              onChange={(e) => setWalletName(e.target.value)}
                              placeholder="Name a new wallet (optional) — e.g. Trading"
                              maxLength={50}
                              disabled={busy}
                              aria-label="New wallet name (optional)"
                              style={{
                                width: '100%', padding: '8px 10px', borderRadius: 8,
                                background: 'var(--surface-1)', border: '1px solid var(--glass-border)',
                                color: 'var(--text-1)', fontSize: '0.85rem',
                              }}
                            />
                            {walletName.trim().length > 0 && sanitizeWalletName(walletName) === null && (
                              <span style={{ fontSize: '0.7rem', color: 'var(--negative)' }}>
                                At least 5 characters — letters, numbers, or _ @ . : + -
                              </span>
                            )}
                          </div>
                          <div style={{ display: 'flex', gap: 8 }}>
                            <button
                              type="button"
                              className="btn btn-primary"
                              style={{ flex: 1 }}
                              onClick={() => handleConnect(p.id, { mode: 'login' })}
                              disabled={busy}
                            >
                              Open existing wallet
                            </button>
                            <button
                              type="button"
                              className="btn btn-outline"
                              style={{ flex: 1 }}
                              onClick={() => handleConnect(p.id, { mode: 'register', walletName })}
                              disabled={busy || (walletName.trim().length > 0 && sanitizeWalletName(walletName) === null)}
                            >
                              Create new wallet
                            </button>
                          </div>
                          <span style={{ fontSize: '0.72rem', color: 'var(--text-3)', lineHeight: 1.4 }}>
                            <strong style={{ color: 'var(--text-3)' }}>Open existing wallet</strong> opens your passkey
                            picker — choose the wallet you want. <strong style={{ color: 'var(--text-3)' }}>Create new</strong> mints a fresh wallet under the name above (or an auto-generated one).
                          </span>
                        </div>
                      ) : (
                        <button type="button" className="wallet-option" onClick={() => handleConnect(p.id)} disabled={busy}>
                          {p.iconDataUri ? (
                            <img src={p.iconDataUri} alt="" width={20} height={20} style={{ borderRadius: 4 }} />
                          ) : (
                            <span className={`wallet-icon ${p.icon} w-5 h-5`} />
                          )}
                          <span>{p.name}</span>
                        </button>
                      )}
                    </Fragment>
                  )
                })}
              </div>
            )}

            {error && <div className="status mt-3" role="alert">{error}</div>}

            <button type="button" className="btn btn-outline mt-3 w-full" onClick={() => setShowModal(false)}>Cancel</button>
          </div>
        </div>,
        document.body
      )}
    </>
  )
}
