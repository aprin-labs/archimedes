import { useState, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import {
  getWalletClient,
  getAddress,
  getConnectedProvider,
  getSmartAccount,
  getSmartAccountClient,
  publicClient,
  USDC,
  TOKEN_ABI,
  VAULT_ABI,
  ASSETS,
  CIRCLE_PROVIDER_ID,
  USDC_DECIMALS,
} from '../config'
import { isAddress, parseUnits } from 'viem'
import { executeUserOp, encodeCall } from '../circle-tx-executor'
import { canStore } from '../storage-consent.js'

const ARCSCAN_TX = 'https://testnet.arcscan.app/tx'
const STORAGE_PREFIX = 'archimedes_deposit_'

// Step states
const PENDING = 'pending'
const WAITING = 'waiting'      // wallet prompt shown
const CONFIRMING = 'confirming' // tx submitted, awaiting confirmation
const DONE = 'done'
const FAILED = 'failed'

const STEPS = [
  { key: 'approve', label: 'Approve USDC', desc: 'Grant the vault permission to receive your USDC' },
  { key: 'deposit', label: 'Deposit', desc: 'Deposit USDC into the ERC-4626 vault' },
  { key: 'allocate', label: 'Set Allocations', desc: 'Configure the target portfolio allocation' },
]

function loadProgress(vaultAddress) {
  try {
    const raw = localStorage.getItem(`${STORAGE_PREFIX}${vaultAddress}`)
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

function saveProgress(vaultAddress, stepIndex, txHashes) {
  try {
    // Functional category (#1647): resume-after-reload is convenience, not
    // correctness. With functional storage rejected the step list restarts on
    // a reload; transactions already broadcast are untouched either way.
    const key = `${STORAGE_PREFIX}${vaultAddress}`
    if (canStore(key)) localStorage.setItem(key, JSON.stringify({ stepIndex, txHashes }))
  } catch { /* storage unavailable */ }
}

function clearProgress(vaultAddress) {
  try { localStorage.removeItem(`${STORAGE_PREFIX}${vaultAddress}`) } catch { /* */ }
}

function shortHash(hash) {
  if (!hash) return ''
  return `${hash.slice(0, 10)}…${hash.slice(-6)}`
}

function StatusIcon({ status }) {
  if (status === DONE) return <span className="i-lucide-check w-[1.1rem] h-[1.1rem]" style={{ color: 'var(--positive, #22c55e)' }} />
  if (status === FAILED) return <span className="i-lucide-x w-[1.1rem] h-[1.1rem]" style={{ color: 'var(--negative, #ef4444)' }} />
  if (status === CONFIRMING) return <span className="spin" style={{ display: 'inline-block', width: 16, height: 16, border: '2px solid var(--text-4)', borderTopColor: 'var(--accent)', borderRadius: '50%', animation: 'spin 0.8s linear infinite' }} />
  if (status === WAITING) return <span className="i-lucide-hourglass w-[1.1rem] h-[1.1rem]" style={{ color: 'var(--accent)' }} />
  return <span className="i-lucide-circle w-[1.1rem] h-[1.1rem]" style={{ color: 'var(--text-3)' }} />
}

// Marker so the catch blocks can tell a bad-input parse failure apart from an
// on-chain approve/deposit revert and show "Invalid amount" rather than the
// misleading "Approval failed" / "Deposit failed" path.
const INVALID_AMOUNT_MESSAGE = 'Invalid amount'

// Normalize a user-typed USDC amount to at most USDC_DECIMALS (6) decimals and
// hand it to parseUnits. viem's parseUnits throws on >6-decimal or
// scientific-notation input; both cases used to surface as "Approval failed".
// We round the fractional part DOWN to 6 places (never mint more than typed)
// and reject anything that isn't a plain decimal number with a clear message.
function parseUsdcAmount(amount) {
  const trimmed = (amount ?? '').toString().trim()
  // Plain decimal only: digits, optional single '.', at least one digit.
  // Rejects scientific notation ("1e5"), signs, and stray characters.
  if (!/^\d+(\.\d+)?$|^\.\d+$|^\d+\.$/.test(trimmed)) {
    throw new Error(INVALID_AMOUNT_MESSAGE)
  }
  const [whole, frac = ''] = trimmed.split('.')
  // Truncate (round down) the fractional part to the token's decimal precision.
  const truncatedFrac = frac.slice(0, USDC_DECIMALS)
  const normalized = truncatedFrac ? `${whole || '0'}.${truncatedFrac}` : (whole || '0')
  try {
    return parseUnits(normalized, USDC_DECIMALS)
  } catch {
    // Defensive: anything parseUnits still rejects is an invalid amount, not a
    // transaction failure.
    throw new Error(INVALID_AMOUNT_MESSAGE)
  }
}

// Default allocation: equal weight across first 4 synthetics + remainder USDC
function defaultAllocations() {
  const tokens = ASSETS.slice(0, 4)
  const weightPer = Math.floor(7500 / tokens.length) // 75% total across synthetics
  return {
    tokens: tokens.map(a => a.token),
    weights: tokens.map(() => BigInt(weightPer)),
    labels: tokens.map(a => `${a.sym} (${(weightPer / 100).toFixed(0)}%)`),
  }
}

export default function DepositFlow(props) {
  // Branch on wallet type: passkey wallets sign ONE batched user operation
  // (approve + deposit + setTargetAllocations in a single biometric
  // confirmation, gas sponsored by Circle Gas Station). EOA wallets keep
  // the existing 3-step stepper since each writeContract call gets its
  // own wallet popup either way.
  if (getConnectedProvider() === CIRCLE_PROVIDER_ID) {
    return <PasskeyDepositFlow {...props} />
  }
  return <EoaDepositFlow {...props} />
}

function EoaDepositFlow({ vaultAddress, depositAmount = '100', strategy, onClose, onComplete }) {
  // Resume from localStorage if we have prior progress
  const saved = loadProgress(vaultAddress)
  const [currentStep, setCurrentStep] = useState(saved?.stepIndex ?? 0)
  const [txHashes, setTxHashes] = useState(saved?.txHashes ?? [])
  const [statuses, setStatuses] = useState(() => {
    const s = [PENDING, PENDING, PENDING]
    if (saved?.stepIndex > 0) { for (let i = 0; i < saved.stepIndex; i++) s[i] = DONE }
    if (saved?.stepIndex < 3) s[saved.stepIndex] = PENDING
    return s
  })
  const [errors, setErrors] = useState([null, null, null])
  const [amount, setAmount] = useState(depositAmount)

  // Persist progress on every state change
  useEffect(() => {
    saveProgress(vaultAddress, currentStep, txHashes)
  }, [currentStep, txHashes, vaultAddress])

  const updateStep = useCallback((index, field, value) => {
    if (field === 'status') {
      setStatuses(prev => { const n = [...prev]; n[index] = value; return n })
    }
    if (field === 'error') {
      setErrors(prev => { const n = [...prev]; n[index] = value; return n })
    }
    if (field === 'txHash') {
      setTxHashes(prev => { const n = [...prev]; n[index] = value; return n })
    }
  }, [])

  // ── Step 1: USDC.approve(vault, amount) ──────────────────
  const runApprove = useCallback(async () => {
    updateStep(0, 'status', WAITING)
    updateStep(0, 'error', null)
    try {
      // Defense-in-depth (audit 2026-06-14): never route a USDC approve to an
      // unvalidated spender. Mirrors the VaultDetail.jsx guard — DepositFlow is
      // the primary funding modal and must not grant allowance to a malformed
      // address if a future caller passes one.
      if (!isAddress(vaultAddress)) throw new Error('Invalid vault address — refusing to send funds.')
      const walletClient = await getWalletClient()
      const parsedAmount = parseUsdcAmount(amount)
      if (parsedAmount <= 0n) throw new Error(INVALID_AMOUNT_MESSAGE)

      const hash = await walletClient.writeContract({
        address: USDC,
        abi: TOKEN_ABI,
        functionName: 'approve',
        args: [vaultAddress, parsedAmount],
      })
      updateStep(0, 'txHash', hash)
      updateStep(0, 'status', CONFIRMING)

      // viem does NOT throw on a mined-but-reverted tx — it returns a receipt
      // with status 'reverted'. Check it, or a reverted approve would read as
      // "Confirmed" and the flow would advance as if funds moved.
      const receipt = await publicClient.waitForTransactionReceipt({ hash })
      if (receipt.status !== 'success') {
        throw new Error(`Approval reverted on-chain (${shortHash(hash)})`)
      }

      updateStep(0, 'status', DONE)
      setCurrentStep(1)
    } catch (err) {
      updateStep(0, 'status', FAILED)
      updateStep(0, 'error', err.shortMessage || err.message || 'Approval failed')
    }
  }, [amount, vaultAddress, updateStep])

  // ── Step 2: vault.deposit(amount, receiver) ──────────────
  const runDeposit = useCallback(async () => {
    updateStep(1, 'status', WAITING)
    updateStep(1, 'error', null)
    try {
      if (!isAddress(vaultAddress)) throw new Error('Invalid vault address — refusing to send funds.')
      const walletClient = await getWalletClient()
      const userAddr = getAddress()
      if (!userAddr) throw new Error('Wallet address not available')
      const parsedAmount = parseUsdcAmount(amount)

      const hash = await walletClient.writeContract({
        address: vaultAddress,
        abi: VAULT_ABI,
        functionName: 'deposit',
        args: [parsedAmount, userAddr],
      })
      updateStep(1, 'txHash', hash)
      updateStep(1, 'status', CONFIRMING)

      // A reverted deposit still returns a receipt; guard it so we never report
      // the deposit as done when the funds didn't actually move.
      const receipt = await publicClient.waitForTransactionReceipt({ hash })
      if (receipt.status !== 'success') {
        throw new Error(`Deposit reverted on-chain (${shortHash(hash)})`)
      }

      updateStep(1, 'status', DONE)
      setCurrentStep(2)
    } catch (err) {
      updateStep(1, 'status', FAILED)
      updateStep(1, 'error', err.shortMessage || err.message || 'Deposit failed')
    }
  }, [amount, vaultAddress, updateStep])

  // ── Step 3: vault.setTargetAllocations(tokens, weights) ──
  const runAllocate = useCallback(async () => {
    updateStep(2, 'status', WAITING)
    updateStep(2, 'error', null)
    try {
      if (!isAddress(vaultAddress)) throw new Error('Invalid vault address — refusing to send funds.')
      const walletClient = await getWalletClient()
      const { tokens, weights } = defaultAllocations()

      const hash = await walletClient.writeContract({
        address: vaultAddress,
        abi: VAULT_ABI,
        functionName: 'setTargetAllocations',
        args: [tokens, weights],
      })
      updateStep(2, 'txHash', hash)
      updateStep(2, 'status', CONFIRMING)

      // A reverted setTargetAllocations still returns a receipt; guard it so the
      // flow doesn't finish "successfully" with allocations that never set.
      const receipt = await publicClient.waitForTransactionReceipt({ hash })
      if (receipt.status !== 'success') {
        throw new Error(`Allocation reverted on-chain (${shortHash(hash)})`)
      }

      updateStep(2, 'status', DONE)
      clearProgress(vaultAddress)
      // All done
      setCurrentStep(3)
    } catch (err) {
      updateStep(2, 'status', FAILED)
      updateStep(2, 'error', err.shortMessage || err.message || 'Allocation failed')
    }
  }, [vaultAddress, updateStep])

  const runFns = [runApprove, runDeposit, runAllocate]
  const allDone = statuses.every(s => s === DONE) || currentStep >= 3

  const handleClose = () => {
    // Don't clear progress on close — allows resume
    if (onClose) onClose()
  }

  const handleFinish = () => {
    clearProgress(vaultAddress)
    if (onComplete) onComplete()
  }

  return createPortal(
    <div
      className="fixed inset-0 flex items-center justify-center z-[1000]"
      style={{ background: 'rgba(0,0,0,0.78)', backdropFilter: 'blur(6px)' }}
      onClick={handleClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="deposit-flow-title"
    >
      <div
        className="card-elevated p-6 max-w-[580px] w-[92vw]"
        onClick={e => e.stopPropagation()}
        style={{ background: 'var(--surface-1)', maxHeight: '90vh', overflowY: 'auto' }}
      >
        {/* Header */}
        <div className="caption mb-2 uppercase tracking-wider text-[var(--text-3)]">
          {allDone ? 'Deposit complete' : 'Fund your vault'}
        </div>
        <h3 id="deposit-flow-title" className="font-serif text-[1.5rem] mb-1">
          {strategy?.paper_title || 'Strategy Vault'}
        </h3>
        <p className="caption mb-1 mono text-[var(--text-3)]">
          Vault: {vaultAddress?.slice(0, 10)}…{vaultAddress?.slice(-6)}
        </p>
        <p className="caption mb-4 leading-relaxed">
          {allDone
            ? 'Your vault is funded and the target allocation is set. The autonomous agent will begin managing it.'
            : 'Three on-chain transactions to fund and configure your vault. Each is signed by your wallet.'}
        </p>

        {/* Amount input */}
        {!allDone && (
          <div className="mb-4">
            <label className="block">
              <span className="caption block mb-1">Deposit amount (USDC)</span>
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={amount}
                onChange={e => setAmount(e.target.value)}
                className="chat-input w-full p-2.5 mono"
                disabled={statuses[0] === DONE} // lock after approve
              />
            </label>
          </div>
        )}

        {/* Stepper */}
        <div className="flex flex-col gap-0">
          {STEPS.map((step, i) => {
            const status = statuses[i]
            const isActive = i === currentStep && !allDone
            const isPast = statuses[i] === DONE
            const isFailed = statuses[i] === FAILED
            const txHash = txHashes[i]

            return (
              <div key={step.key} style={{ opacity: isActive || isPast || isFailed ? 1 : 0.45 }}>
                {/* Step row */}
                <div className="flex items-start gap-3 py-3" style={{
                  borderBottom: i < 2 ? '1px solid var(--glass-border, rgba(255,255,255,0.08))' : 'none',
                }}>
                  <div className="flex flex-col items-center pt-0.5" style={{ minWidth: 24 }}>
                    <StatusIcon status={status} />
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="body" style={{ fontWeight: isActive ? 600 : 400 }}>
                        {i + 1}. {step.label}
                      </span>
                      {txHash && (
                        <a
                          href={`${ARCSCAN_TX}/${txHash}`}
                          target="_blank"
                          rel="noreferrer"
                          className="mono caption"
                          style={{ color: 'var(--accent)', fontSize: '0.7rem' }}
                        >
                          {shortHash(txHash)} ↗
                        </a>
                      )}
                    </div>
                    <p className="caption mt-0.5 text-[var(--text-3)]">{step.desc}</p>

                    {/* Status details */}
                    {status === CONFIRMING && (
                      <p className="caption mt-1" style={{ color: 'var(--accent)' }}>
                        Confirming on Arc…
                      </p>
                    )}
                    {status === FAILED && errors[i] && (
                      <div className="mt-2">
                        <p className="caption" style={{ color: 'var(--negative, #ef4444)' }}>
                          {errors[i]}
                        </p>
                        <button
                          className="btn btn-outline btn-sm mt-1.5"
                          onClick={() => runFns[i]()}
                          style={{ fontSize: '0.75rem' }}
                        >
                          Retry
                        </button>
                      </div>
                    )}
                    {isActive && status === PENDING && (
                      <button
                        className="btn btn-primary btn-sm mt-2"
                        onClick={() => runFns[i]()}
                      >
                        {i === 0 ? 'Approve USDC' : i === 1 ? 'Deposit' : 'Set Allocations'}
                      </button>
                    )}
                    {status === DONE && (
                      <p className="caption mt-1" style={{ color: 'var(--positive, #22c55e)' }}>
                        Confirmed {txHash ? `· ${shortHash(txHash)}` : ''}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* Allocation detail (collapsible) */}
        {!allDone && currentStep >= 1 && (
          <div className="mt-3 card-flat p-3">
            <div className="caption mb-1 text-[var(--text-3)]">Target allocation (equal weight)</div>
            <div className="flex flex-wrap gap-2">
              {defaultAllocations().labels.map((label, i) => (
                <span key={i} className="tag tag-muted" style={{ fontSize: '0.7rem' }}>{label}</span>
              ))}
              <span className="tag tag-muted" style={{ fontSize: '0.7rem' }}>USDC (25%)</span>
            </div>
          </div>
        )}

        {/* Completion CTA */}
        {allDone && (
          <div className="mt-5 flex justify-end">
            <button className="btn btn-primary" onClick={handleFinish}>
              Done — View Portfolio →
            </button>
          </div>
        )}

        {/* Close / cancel */}
        {!allDone && (
          <div className="flex justify-end gap-2 mt-5">
            <button className="btn btn-outline" onClick={handleClose}>
              {statuses.some(s => s === DONE) ? 'Continue Later' : 'Cancel'}
            </button>
          </div>
        )}
      </div>

      {/* Spinner keyframes (injected once) */}
      <style>{`
        @keyframes spin { to { transform: rotate(360deg) } }
        .spin { animation: spin 0.8s linear infinite; }
      `}</style>
    </div>,
    document.body,
  )
}

// ── Passkey-wallet variant: single batched user operation ──────────────
//
// Submits approve + deposit + setTargetAllocations as ONE user op via
// Circle's bundler. Single biometric prompt for the whole sequence;
// gas sponsored by Circle Gas Station (paymaster: true). Much smoother
// UX than the 3-popup EOA flow — sell this in the pitch deck.
//
// State machine:
//   IDLE     — form rendered; awaiting user "Confirm deposit" click
//   SIGNING  — WebAuthn prompt up; user is signing the user op
//   SENT     — user op submitted to bundler; awaiting on-chain inclusion
//   COMPLETE — on-chain success; arcscan link rendered
//   FAILED   — error mapped to a friendly message via the executor
function PasskeyDepositFlow({ vaultAddress, depositAmount = '100', strategy, onClose, onComplete }) {
  const [amount, setAmount] = useState(depositAmount)
  const [state, setState] = useState('IDLE')
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)  // { userOpHash, txHash }

  const runDeposit = useCallback(async () => {
    setError('')
    setResult(null)
    setState('SIGNING')
    try {
      if (!isAddress(vaultAddress)) throw new Error('Invalid vault address — refusing to send funds.')
      const smartAccount = getSmartAccount()
      const client = getSmartAccountClient()
      const userAddr = getAddress()
      if (!smartAccount || !client) {
        throw new Error('Passkey wallet not initialized — please reconnect.')
      }
      const parsedAmount = parseUsdcAmount(amount)
      if (parsedAmount <= 0n) throw new Error(INVALID_AMOUNT_MESSAGE)

      const { tokens, weights } = defaultAllocations()

      // Batch all 3 calls into ONE user operation. The bundler executes
      // them atomically in order; if any reverts, all revert.
      const calls = [
        encodeCall({
          address: USDC,
          abi: TOKEN_ABI,
          functionName: 'approve',
          args: [vaultAddress, parsedAmount],
        }),
        encodeCall({
          address: vaultAddress,
          abi: VAULT_ABI,
          functionName: 'deposit',
          args: [parsedAmount, userAddr],
        }),
        encodeCall({
          address: vaultAddress,
          abi: VAULT_ABI,
          functionName: 'setTargetAllocations',
          args: [tokens, weights],
        }),
      ]

      const out = await executeUserOp({
        smartAccount,
        client,
        calls,
        onStateChange: setState,
      })
      setResult(out)
      setState('COMPLETE')
    } catch (err) {
      setState('FAILED')
      setError(err.message || 'Deposit failed')
    }
  }, [amount, vaultAddress])

  const isDone = state === 'COMPLETE'
  const isBusy = state === 'SIGNING' || state === 'SENT'

  return createPortal(
    <div
      className="fixed inset-0 flex items-center justify-center z-[1000]"
      style={{ background: 'rgba(0,0,0,0.78)', backdropFilter: 'blur(6px)' }}
      onClick={isBusy ? undefined : onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="passkey-deposit-title"
    >
      <div
        className="card-elevated p-6 max-w-[520px] w-[92vw]"
        onClick={e => e.stopPropagation()}
        style={{ background: 'var(--surface-1)', maxHeight: '90vh', overflowY: 'auto' }}
      >
        <div className="caption mb-2 uppercase tracking-wider text-[var(--text-3)]">
          {isDone ? 'Deposit complete' : 'Fund your vault'}
        </div>
        <h3 id="passkey-deposit-title" className="font-serif text-[1.5rem] mb-1">
          {strategy?.paper_title || 'Strategy Vault'}
        </h3>
        <p className="caption mb-1 mono text-[var(--text-3)]">
          Vault: {vaultAddress?.slice(0, 10)}…{vaultAddress?.slice(-6)}
        </p>
        <p className="caption mb-4 leading-relaxed">
          {isDone
            ? 'Your vault is funded and the target allocation is set. The autonomous agent will begin managing it.'
            : 'Approve USDC, deposit, and set target allocations — batched into one gasless transaction signed with your passkey.'}
        </p>

        {!isDone && (
          <div className="mb-4">
            <label className="block">
              <span className="caption block mb-1">Deposit amount (USDC)</span>
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={amount}
                onChange={e => setAmount(e.target.value)}
                className="chat-input w-full p-2.5 mono"
                disabled={isBusy}
              />
            </label>
          </div>
        )}

        {/* What this batches — surface honestly so users know what they're signing */}
        {!isDone && (
          <div className="card-flat p-3 mb-3">
            <div className="caption mb-2 text-[var(--text-3)]">
              One passkey signature authorizes:
            </div>
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              <li className="caption" style={{ marginBottom: 4 }}>
                Approve {amount} USDC for the vault
              </li>
              <li className="caption" style={{ marginBottom: 4 }}>
                Deposit into the ERC-4626 vault
              </li>
              <li className="caption" style={{ marginBottom: 4 }}>
                Set target allocation across {defaultAllocations().labels.length} synthetics
              </li>
            </ul>
            <div className="caption mt-2" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>
              Gas sponsored by Circle Gas Station — you pay $0.
            </div>
          </div>
        )}

        {/* In-flight state */}
        {state === 'SIGNING' && (
          <div className="info-box mt-3" style={{ background: 'var(--accent-muted)' }}>
            <span className="caption">Waiting for passkey confirmation… (Touch ID / Face ID / hardware key)</span>
          </div>
        )}
        {state === 'SENT' && (
          <div className="info-box mt-3" style={{ background: 'var(--accent-muted)' }}>
            <span className="caption">User operation submitted. Awaiting on-chain confirmation…</span>
          </div>
        )}

        {/* Completion */}
        {isDone && result?.txHash && (
          <div className="card-flat p-3 mt-3">
            <div className="caption mb-1 text-[var(--text-3)]">Confirmed on Arc Testnet</div>
            <a
              href={`${ARCSCAN_TX}/${result.txHash}`}
              target="_blank"
              rel="noreferrer"
              className="mono caption"
              style={{ color: 'var(--accent)', fontSize: '0.75rem' }}
            >
              {shortHash(result.txHash)} ↗
            </a>
          </div>
        )}

        {state === 'FAILED' && error && (
          <div className="info-box warning mt-3">
            <span className="caption">{error}</span>
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-2 mt-5">
          {!isDone && (
            <>
              <button className="btn btn-outline" onClick={onClose} disabled={isBusy}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={runDeposit}
                disabled={isBusy}
              >
                {state === 'SIGNING' ? 'Confirming…'
                  : state === 'SENT' ? 'Awaiting on-chain…'
                  : state === 'FAILED' ? 'Retry'
                  : 'Confirm deposit'}
              </button>
            </>
          )}
          {isDone && (
            <button className="btn btn-primary" onClick={onComplete}>
              Done — View Portfolio →
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
