import { useState, useEffect, useCallback } from 'react'
import { apiGet } from '../api'
import { ROADMAP_SURFACES_ENABLED } from '../featureFlags.js'
import { insights as ROADMAP_COPY } from '../roadmapCopyApp.js'

// Insights — the owner's admin-only traction + engagement dashboard
// (#787, #830, #854; dashboard v2 + admin gate: owner directive 2026-08-20).
//
// OWNER DIRECTIVE (2026-08-20) — SUPERSEDES issue #1028 D8 "public Insights
// page": /app/insights is no longer on the public app surface. It is
// ADMIN-ONLY (PLATFORM_ADMIN_WALLETS holders), gated server-side by
// GET /api/metrics/private/whoami (backend/archimedes/api/metrics_private_routes.py)
// and, on the frontend, by App.jsx's probe-on-entry (ui/src/adminProbe.js) —
// a non-admin/anonymous visitor never reaches this component at all; they get
// the exact same treatment as an unknown route (ui/src/components/NotFound.jsx),
// not a "you need admin access" message that would advertise the page exists.
// This component can therefore assume every render is an authenticated admin.
//
// Renders the live conversion + engagement instruments as a human-readable
// dashboard instead of raw JSON:
//   - Traction: human vs agent REQUEST counts (honestly labelled — these are
//     cumulative request tallies, bot-inflated, NOT unique users) + the honest
//     canonical Better Auth account count alongside them. "Real users (accounts)"
//     is an all-time count; it is a DIFFERENT population from
//     the funnel's "Connected Wallet" below (distinct anonymous visitor
//     SESSIONS that reached that step) — the two numbers are not expected to
//     match, and the copy says so explicitly rather than leaving readers to
//     guess.
//   - Conversion funnel: distinct visitors through landed → generation_started
//     → free_generation_used → wallet_gate_shown → wallet_connected →
//     vault_deployed, with step-conversion %. (#1643 reordered this: the first
//     three generations on an account need no wallet, so a visitor generates
//     BEFORE connecting one. The labels below only rename the backend's
//     STAGES; the order is the backend's.)
//   - Visitor insights: distinct visitors by country + device, drawn from the
//     SAME JS-gated `landed` beacon population as the funnel, attributed once
//     per visitor (issue #854 finding #6: a Redis SADD first-seen gate in
//     services/visitor_insights_store.py). This is INTENDED to sum to the
//     funnel's `landed` count going forward, but the underlying HyperLogLog
//     counters are append-only — visits recorded before the attribution gate
//     shipped (2026-07-03) remain baked into the all-time totals and can't be
//     retroactively de-duplicated without an operator resetting the Redis
//     keys. The UI states this as a directional relationship, not an exact
//     equality, until that reset happens. Geography is `ZZ` until Dan's
//     CloudFront terraform apply lands (#795). Country breakdown uses
//     Intl.DisplayNames (countryName() below) — no hand-maintained map, no
//     new dep; a world map beside it was evaluated and deferred (see the
//     PR body's Phase 2 section: a faithful map needs real geometry data,
//     which is not a "small hand-bundled inline SVG" at zero new deps).
//   - Engagement & adoption (dashboard v2, admin-only, current schema):
//     accounts, linked wallets, strategies generated + trend, generation-cost
//     token totals, paper-deployment status, and a repeat-generator proxy —
//     GET /api/metrics/private/engagement (services/engagement_metrics.py).
//     Money paid is DRY-RUN only today (PAYMENTS_DRY_RUN) — no settled volume
//     to report yet; see that endpoint's docstring for exactly what is/isn't
//     joinable and the PR body for the Phase 2 deferred-metric list.
//   - The Bedrock/infra COST dashboard (GET /api/metrics/private/cost) is a
//     SEPARATE surface and stays intentionally NOT rendered here — its fields
//     are still draft placeholders pending live billing wiring, not something
//     this page should present as a real number.

// The backend funnel keeps tracking `vault_deployed` regardless of this
// flag (it's a real server-side stage, see /api/metrics/funnel); the label
// — and the row itself — is gated so a public dashboard doesn't hold out a
// permanently 0/0% roadmap stage as a tracked product goal (#1354).
// Key order matters: the funnel API returns stages in backend STAGES order and
// this map only renames them, so the two must describe the same journey (#1643
// put generation ahead of the wallet — the first three generations are free).
const CORE_FUNNEL_LABELS = {
  landed: 'Landed',
  generation_started: 'Tried Generate',
  free_generation_used: 'Used a Free Generation',
  wallet_gate_shown: 'Hit the Wallet Gate',
  wallet_connected: 'Connected Wallet',
}
const FUNNEL_LABELS = ROADMAP_SURFACES_ENABLED
  ? { ...CORE_FUNNEL_LABELS, vault_deployed: ROADMAP_COPY.vaultDeployedLabel }
  : CORE_FUNNEL_LABELS

const DEVICE_LABELS = { mobile: 'Mobile', tablet: 'Tablet', desktop: 'Desktop', tv: 'TV', unknown: 'Unknown' }

// Built-in Intl API resolves any ISO-3166 alpha-2 code to a display name —
// no dependency, no hand-maintained (and inevitably incomplete) map (#1366
// AC4). ZZ needs an explicit case: Intl.DisplayNames renders it as "Unknown
// Region", which would contradict the "unknown / not provided" wording this
// page already uses for ZZ (see the visitor-insights caption below).
const REGION_NAMES = new Intl.DisplayNames(['en'], { type: 'region' })

function countryName(code) {
  if (code === 'ZZ') return 'Unknown / not provided'
  try {
    return REGION_NAMES.of(code) || code
  } catch {
    return code
  }
}

// Labels for the per-stage by_agent_type split the funnel API already
// returns (issue #788) and this page now renders instead of describing a
// filter the write path doesn't perform (#1366 AC3).
const AGENT_TYPE_LABELS = { human: 'Human', external: 'External bot', internal: 'Internal' }

const card = {
  background: 'var(--surface-1)',
  border: '1px solid var(--glass-border)',
  borderRadius: 12,
  padding: '20px 22px',
}

function Bar({ pct, color = '#5b9dff' }) {
  return (
    <div style={{ background: 'var(--glass-hover)', borderRadius: 6, height: 10, overflow: 'hidden' }}>
      <div style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: color, height: '100%', transition: 'width .3s' }} />
    </div>
  )
}

export default function Insights() {
  const [metrics, setMetrics] = useState(null)
  const [funnel, setFunnel] = useState(null)
  const [visitors, setVisitors] = useState(null)
  const [engagement, setEngagement] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [visitorsError, setVisitorsError] = useState(null)
  const [engagementError, setEngagementError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setVisitorsError(null)
    setEngagementError(null)
    // Core dashboard = metrics + funnel; visitor-insights and the (admin-only)
    // engagement snapshot each load independently so a transient failure in
    // ONE (500/network) degrades to that card's own error/empty state instead
    // of blanking the whole dashboard — per-endpoint resilience (#830 review),
    // now extended to the dashboard-v2 tile.
    try {
      const [m, f] = await Promise.all([
        apiGet('/api/metrics'),
        apiGet('/api/metrics/funnel'),
      ])
      setMetrics(m)
      setFunnel(f)
    } catch (e) {
      setError(String(e.message || e))
    }
    try {
      const v = await apiGet('/api/metrics/visitors')
      setVisitors(v)
    } catch (e) {
      setVisitors(null)
      setVisitorsError(String(e.message || e))
    }
    try {
      const e2 = await apiGet('/api/metrics/private/engagement')
      setEngagement(e2)
    } catch (e) {
      setEngagement(null)
      setEngagementError(String(e.message || e))
    }
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  // null (not 0) when the funnel hasn't loaded / failed — so "Distinct visitors"
  // renders an honest "—" (unknown) rather than a misleading "0".
  const landed = funnel ? (funnel.stages?.find(s => s.stage === 'landed')?.distinct_visitors ?? 0) : null
  const totalDevices = visitors ? Object.values(visitors.devices || {}).reduce((a, b) => a + b, 0) : 0
  const maxCountry = visitors?.countries?.[0]?.distinct_visitors ?? 0
  // Formatted, not hard-coded (#1366 AC4) — metrics.epoch_started_at is the
  // real durable-counting start time; null only when no snapshot has ever
  // been recorded, in which case we say so rather than guessing a date.
  const epochStarted = metrics?.epoch_started_at
    ? new Date(metrics.epoch_started_at).toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' })
    : null

  return (
    <div style={{ maxWidth: 880, margin: '0 auto', padding: '8px 4px 48px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <h1 style={{ margin: '0 0 4px' }}>Insights</h1>
          <span
            style={{
              fontSize: 10.5,
              fontWeight: 700,
              letterSpacing: 0.4,
              padding: '2px 8px',
              borderRadius: 999,
              background: 'var(--glass-hover)',
              color: 'var(--text-2)',
            }}
          >
            ADMIN ONLY
          </span>
        </div>
        <button onClick={load} disabled={loading} style={{ fontSize: 13, padding: '6px 12px', borderRadius: 8, cursor: 'pointer' }}>
          {loading ? 'Refreshing…' : '↻ Refresh'}
        </button>
      </div>
      <p style={{ color: 'var(--text-2)', marginTop: 0, fontSize: 14 }}>
        Read-only conversion, traffic, and engagement metrics — the owner traction dashboard.
      </p>

      {error && (
        <div role="alert" style={{ ...card, borderColor: 'var(--negative-bd)', color: 'var(--negative)', marginBottom: 16 }}>
          Couldn’t load metrics: {error}
        </div>
      )}

      {/* ── Real people (the honest headline) — distinct people first; raw request
          volume is demoted to a footnote because it's bot-inflated server hits, not people. */}
      <section style={{ ...card, marginBottom: 16 }}>
        <h2 style={{ marginTop: 0, fontSize: 16 }}>Real people</h2>
        <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
          <Stat label="Distinct visitors" value={landed} accent="#5b9dff" />
          <Stat label="Real users (accounts)" value={metrics?.real_users} accent="#3fb56b" />
        </div>
        <p style={{ color: 'var(--text-2)', fontSize: 12.5, marginBottom: 0, marginTop: 14 }}>
          The honest “how many people” numbers. <strong>Distinct visitors</strong> = every browser session
          that loaded the app (JS-gated) — this is the funnel’s <em>Landed</em> count below, and it still
          includes sessions the telemetry classifier positively tags as agents. See the human / external
          bot / internal split under each stage below for exactly how many.
          <strong> Real users</strong> = canonical accounts, all-time. This differs from
          “Connected Wallet”, which counts visitor sessions reaching optional wallet-link step.
        </p>
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--glass-border)', fontSize: 12, color: 'var(--text-3)' }}>
          <strong style={{ color: 'var(--text-2)' }}>Raw request volume</strong> — server hits, <em>not</em> people
          (cumulative all-time, heavily bot-inflated; kept only as an infra signal):{' '}
          {metrics?.total_requests?.toLocaleString() ?? '—'} total ·{' '}
          {metrics?.human_count?.toLocaleString() ?? '—'} browser-UA ·{' '}
          {metrics?.agent_count?.toLocaleString() ?? '—'} agent/bot.
        </div>
      </section>

      {/* ── Engagement & adoption (dashboard v2, admin-only, #1354-successor) ──
          Current-schema reads only — every field here is a real query against
          an existing table (services/engagement_metrics.py), never a sample
          or an estimate. Anything the current schema can't yet join honestly
          is listed in the PR body's Phase 2 section instead of appearing
          here as a guess. */}
      <section style={{ ...card, marginBottom: 16 }}>
        <h2 style={{ marginTop: 0, fontSize: 16 }}>Engagement &amp; adoption</h2>
        <p style={{ color: 'var(--text-2)', fontSize: 12.5, marginTop: 0, marginBottom: 14 }}>
          Admin-only. Current-schema reads — no sampling, no estimation.
        </p>
        {loading && !engagement && !engagementError ? (
          <Empty>Loading engagement metrics…</Empty>
        ) : engagementError ? (
          <Empty>Couldn’t load engagement metrics: {engagementError}</Empty>
        ) : !engagement ? (
          <Empty>No engagement data yet.</Empty>
        ) : (
          <>
            <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 18 }}>
              <Stat label="Accounts (total)" value={engagement.accounts?.total} accent="#3fb56b" />
              <Stat label="New accounts (7d)" value={engagement.accounts?.new_7d} accent="#5b9dff" />
              <Stat label="New accounts (30d)" value={engagement.accounts?.new_30d} accent="#5b9dff" />
              <Stat label="Linked wallets" value={engagement.linked_wallets?.total} accent="#7c6cff" />
            </div>

            <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 8 }}>
              <Stat label="Strategies generated (total)" value={engagement.strategies?.total} accent="#3fb56b" />
              <Stat label="Generated (7d)" value={engagement.strategies?.new_7d} accent="#5b9dff" />
              <Stat
                label="Repeat generators (proxy)"
                value={
                  engagement.repeat_generation_users?.repeat_users == null ||
                  engagement.repeat_generation_users?.generating_users == null
                    ? null
                    : `${engagement.repeat_generation_users.repeat_users} / ${engagement.repeat_generation_users.generating_users}`
                }
                accent="#f0a63a"
              />
            </div>
            {/* Both tiles above count distinct strategy_store rows / accounts owning
                them, not generation events — content-hash dedup means a regeneration
                or a second user's identical output doesn't add a new row. Surfaced
                directly under the tile row (round 3 fix), not buried below the
                trend chart, money-paid block, and everything else. */}
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 0, marginBottom: 4 }}>
              {engagement.strategies?.note}
            </p>
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 0, marginBottom: 14 }}>
              <strong>Repeat generators</strong> shown as repeat / generating accounts —{' '}
              {engagement.repeat_generation_users?.note}
            </p>
            {(engagement.strategies?.daily_new?.length ?? 0) > 0 && (
              <div style={{ marginBottom: 18 }}>
                <h3 style={{ fontSize: 12, color: 'var(--text-3)', margin: '0 0 8px' }}>
                  Strategies generated — last 7 days
                </h3>
                <div style={{ display: 'flex', gap: 6, alignItems: 'flex-end', height: 52 }}>
                  {(() => {
                    const days = engagement.strategies.daily_new
                    const max = Math.max(1, ...days.map((d) => d.count))
                    return days.map((d) => (
                      <div
                        key={d.date}
                        title={`${d.date}: ${d.count}`}
                        style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}
                      >
                        <div
                          style={{
                            width: '100%',
                            height: Math.max(2, Math.round((d.count / max) * 40)),
                            background: d.count > 0 ? '#5b9dff' : 'var(--glass-hover)',
                            borderRadius: 3,
                          }}
                        />
                        <span style={{ fontSize: 9, color: 'var(--text-3)' }}>{d.date.slice(5)}</span>
                      </div>
                    ))
                  })()}
                </div>
              </div>
            )}

            <div
              style={{
                display: 'flex',
                gap: 28,
                flexWrap: 'wrap',
                marginBottom: 6,
              }}
            >
              <Stat label="Measured generations" value={engagement.generation_costs?.measured_count} accent="#3fb56b" />
              {/* Round 4 fix: the old "Total" framing implied an all-time
                  platform token total. generation_costs only carries a row
                  for a job that
                  persisted >=1 strategy — a generation that consumed tokens
                  but errored/was cancelled/failed the rigor gate first
                  leaves no row at all, not a zero one. Relabelled + the
                  coverage caveat (engagement.generation_costs.note) rendered
                  directly under the tile row so it can't be mistaken for a
                  platform-wide count. */}
              <Stat label="LLM tokens (measured jobs)" value={engagement.generation_costs?.total_tokens} accent="#5b9dff" />
              <Stat label="Paper trading — active" value={engagement.paper_deployments?.active} accent="#3fb56b" />
              <Stat label="Paper trading — stopped" value={engagement.paper_deployments?.stopped} accent="#8a8f9c" />
            </div>
            {engagement.generation_costs?.note && (
              <p
                style={{
                  fontSize: 11,
                  color: 'var(--text-3)',
                  marginTop: 0,
                  marginBottom: engagement.generation_costs?.usage_complete === false ? 4 : 18,
                }}
              >
                {engagement.generation_costs.note}
              </p>
            )}
            {/* usage_complete is false when some LLM calls in the summed rows
                reported no usage data (services/cost_meter.py's calls_missing_usage) —
                the totals above still add up their real reported tokens, but are an
                honest UNDERCOUNT, not a complete measurement (claims-must-be-true:
                round 3 fix — a partial measurement must not read as a plain,
                trustworthy number with no qualifier). Round 4: a corrupt/undecodable
                row now also counts toward calls_missing_usage (see the backend
                docstring), so this qualifier now surfaces that case too. */}
            {engagement.generation_costs?.usage_complete === false && (
              <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 0, marginBottom: 18 }}>
                ⚠ <strong>Partial measurement</strong> — {engagement.generation_costs?.calls_missing_usage ?? '—'} LLM
                call(s) reported no usage data; the totals above only include calls that did, so they undercount.
              </p>
            )}

            {/* Money paid — claims-must-be-true: PAYMENTS_DRY_RUN gates every
                settlement path today, so there is no settled volume to
                report. This is the real slot for it, not a placeholder $0. */}
            <div style={{ marginTop: 4, paddingTop: 12, borderTop: '1px solid var(--glass-border)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span
                  style={{
                    fontSize: 10.5,
                    fontWeight: 700,
                    letterSpacing: 0.4,
                    padding: '2px 8px',
                    borderRadius: 999,
                    background: engagement.payments?.dry_run ? 'var(--glass-hover)' : 'var(--positive-bd, var(--glass-hover))',
                    color: 'var(--text-2)',
                  }}
                >
                  {engagement.payments?.dry_run ? 'DRY-RUN' : 'LIVE'}
                </span>
                <strong style={{ fontSize: 13 }}>Money paid</strong>
              </div>
              <div style={{ fontSize: 26, fontWeight: 700, color: 'var(--text-1)' }}>
                {engagement.payments?.settled_volume_usd == null ? '—' : `$${engagement.payments.settled_volume_usd}`}
              </div>
              <p style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 6, marginBottom: 0 }}>
                {engagement.payments?.note}
              </p>
            </div>
          </>
        )}
      </section>

      {/* ── Conversion funnel ── */}
      <section style={{ ...card, marginBottom: 16 }}>
        <h2 style={{ marginTop: 0, fontSize: 16 }}>Conversion funnel — distinct visitors</h2>
        {loading && !funnel ? (
          <Empty role="status">Loading…</Empty>
        ) : !funnel ? (
          <Empty role="alert">Couldn’t load the funnel{error ? `: ${error}` : '.'}</Empty>
        ) : landed === 0 ? (
          <Empty>
            No visitors recorded yet.{' '}
            {epochStarted ? `Durable counting began ${epochStarted}.` : 'Durable counting start date is unknown.'}
          </Empty>
        ) : (
          <div style={{ display: 'grid', gap: 14 }}>
            {funnel.stages
              .filter((s) => ROADMAP_SURFACES_ENABLED || s.stage !== 'vault_deployed')
              .map((s, i) => {
              const bt = s.by_agent_type || {}
              const tagged = (bt.human ?? 0) + (bt.external ?? 0) + (bt.internal ?? 0)
              // HLL estimates on both sides of the subtraction, so floor at 0
              // rather than show a nonsensical negative "unclassified" count.
              const unclassified = Math.max(0, s.distinct_visitors - tagged)
              return (
                <div key={s.stage}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 14, marginBottom: 5 }}>
                    <span>{FUNNEL_LABELS[s.stage] || s.stage}</span>
                    <span style={{ color: 'var(--text-2)' }}>
                      <strong style={{ color: 'var(--text-1)' }}>{s.distinct_visitors}</strong>
                      {i > 0 && <> · {(s.step_conversion * 100).toFixed(0)}% of prev</>}
                    </span>
                  </div>
                  <Bar pct={s.pct_of_landed * 100} color={i === 0 ? '#5b9dff' : s.distinct_visitors > 0 ? '#3fb56b' : '#3a3f4b'} />
                  <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 3 }}>
                    {Object.entries(AGENT_TYPE_LABELS).map(([key, label]) => `${label} ${bt[key] ?? 0}`).join(' · ')}
                    {' · Unclassified '}{unclassified}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* ── Visitor insights (geo + device) ── */}
      <section style={card}>
        <h2 style={{ marginTop: 0, fontSize: 16 }}>Who’s visiting — geography &amp; device</h2>
        <p style={{ color: 'var(--text-2)', fontSize: 12.5, marginTop: 0, marginBottom: 14 }}>
          Same JS-gated <strong>landed</strong> population as the funnel, attributed once per visitor so a
          repeat visit doesn’t get double-counted. These sums are designed to track the funnel’s{' '}
          <em>Landed</em> number above, but may not match it exactly — both are HyperLogLog estimates, and
          visits recorded before once-per-visitor attribution shipped can still show up in the all-time
          totals. Treat this as directional, not a precise reconciliation. Country shows{' '}
          <code>ZZ</code> (unknown / not provided) until the visitor's country is available.
        </p>
        {loading && !visitors && !visitorsError ? (
          <Empty role="status">Loading visitor insights…</Empty>
        ) : visitorsError ? (
          <Empty role="alert">Couldn’t load visitor insights: {visitorsError}</Empty>
        ) : !visitors || ((visitors.countries?.length ?? 0) === 0 && totalDevices === 0) ? (
          <Empty>No visitors recorded yet.</Empty>
        ) : (
          <div className="insights-two-col">
            <div>
              <h3 style={{ fontSize: 13, color: 'var(--text-2)', margin: '0 0 10px' }}>Top countries</h3>
              <div style={{ display: 'grid', gap: 10 }}>
                {(visitors.countries || []).slice(0, 8).map(c => (
                  <div key={c.code}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 4 }}>
                      <span>{countryName(c.code)}</span>
                      <strong>{c.distinct_visitors}</strong>
                    </div>
                    <Bar pct={maxCountry ? (c.distinct_visitors / maxCountry) * 100 : 0} color="#7c6cff" />
                  </div>
                ))}
              </div>
            </div>
            <div>
              <h3 style={{ fontSize: 13, color: 'var(--text-2)', margin: '0 0 10px' }}>Device</h3>
              <div style={{ display: 'grid', gap: 10 }}>
                {Object.entries(visitors.devices || {})
                  .filter(([, n]) => n > 0)
                  .sort((a, b) => b[1] - a[1])
                  .map(([dev, n]) => (
                    <div key={dev}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 4 }}>
                        <span>{DEVICE_LABELS[dev] || dev}</span>
                        <strong>{n}</strong>
                      </div>
                      <Bar pct={totalDevices ? (n / totalDevices) * 100 : 0} color="#3fb56b" />
                    </div>
                  ))}
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

function Stat({ label, value, accent }) {
  const display = value == null ? '—' : (typeof value === 'number' ? value.toLocaleString() : value)
  return (
    <div>
      <div style={{ fontSize: 26, fontWeight: 700, color: accent || 'var(--text-1)' }}>{display}</div>
      <div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{label}</div>
    </div>
  )
}

function Empty({ children, role }) {
  return <p role={role} style={{ color: 'var(--text-2)', fontSize: 13.5, margin: 0 }}>{children}</p>
}
