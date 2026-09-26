/**
 * RigorExplainer — static educational page explaining the four-primitive rigor gate.
 *
 * No API calls — purely static content. Written for non-quant judges who will
 * encounter DSR/PBO terminology and need a plain-English explanation of why
 * these tests matter and how we use them.
 *
 * Per docs/specs/selection-bias-corrections-spec.md: every Tier-1 strategy
 * must pass DSR, PBO, chronological OOS Sharpe, and look-ahead audit.
 */
export default function RigorExplainer() {
  return (
    <div style={{ maxWidth: 760, margin: '0 auto', padding: '32px 24px' }}>
      <div>
        {/* id is the aria-labelledby target for the Library's modal wrapper. */}
        <h1 id="rigor-explainer-title" style={{ fontSize: '1.6rem', marginBottom: 8, fontFamily: 'var(--serif)' }}>
          The Rigor Gate
        </h1>
        <p className="body" style={{ color: 'var(--text-2)', marginBottom: 20, lineHeight: 1.65 }}>
          Every Tier-1 strategy in the Archimedes library must pass four independent
          selection-bias controls before it earns the{' '}
          <span className="tag tag-positive" style={{ fontSize: '0.7rem' }}>Archimedes Verified</span>{' '}
          badge. Here is what each test measures — and why it matters.
        </p>
        <div className="info-box" style={{ marginBottom: 32, fontSize: '0.86rem', lineHeight: 1.6 }}>
          <strong>You choose how strict the gate is for your own deploys</strong> with a 1–5
          strictness slider (1 = Conservative, the Verified bar; 5 = Speculative). A higher
          level lets you deploy statistically weaker strategies into <em>your own</em> vaults —
          it never changes the global Verified badge, which always stays at the strictest level.
          Three checks are <strong>always enforced at every level</strong> and can never be bypassed:
          the look-ahead audit must pass, the out-of-sample Sharpe must be positive, and the
          deflated-Sharpe confidence must be ≥ 0.50. You can trade confidence for breadth — you can
          never fully bypass the gate.
        </div>
      </div>

      {/* Primitive 1 — DSR */}
      <div className="card-elevated mb-6">
        <div className="flex items-center gap-3 mb-4">
          <div style={{
            width: 32, height: 32, borderRadius: '50%', background: 'rgba(99,102,241,0.15)',
            border: '1px solid rgba(99,102,241,0.4)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontWeight: 700, color: 'var(--accent)', flexShrink: 0,
          }}>1</div>
          <div>
            <div className="label">Deflated Sharpe Ratio (DSR)</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>Bailey & López de Prado (2014)</div>
          </div>
          <span className="tag tag-positive" style={{ marginLeft: 'auto' }}>confidence ≥ 0.95 (badge)</span>
        </div>

        <p className="body" style={{ marginBottom: 16, lineHeight: 1.65 }}>
          A Sharpe ratio adjusted for the fact that you tested many strategies and got lucky.
          The more strategies you tested, the more your best result is probably noise —
          a kind of multiple-testing penalty. DSR answers: "If I tested N candidate strategies
          and selected the one with the best in-sample Sharpe, what is the probability the
          true underlying edge is positive?"
        </p>

        <div className="card-flat" style={{ padding: 14, marginBottom: 12 }}>
          <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 6, textTransform: 'uppercase', fontSize: '0.68rem', letterSpacing: '0.06em' }}>
            Formula
          </div>
          <div className="body" style={{ fontFamily: 'var(--mono, monospace)', fontSize: '0.82rem', color: 'var(--text-2)', lineHeight: 1.5 }}>
            z = (SR̂ − SR₀) × √(T−1) / √(1 − γ₃SR̂ + ((γ₄−1)/4)SR̂²)
            <br />
            <span style={{ color: 'var(--text-3)', fontSize: '0.75rem' }}>
              where SR₀ = expected best Sharpe of N iid trials, γ₃ = skewness, γ₄ = raw kurtosis
            </span>
          </div>
          <div className="caption" style={{ marginTop: 10, color: 'var(--text-3)', fontSize: '0.78rem', lineHeight: 1.5 }}>
            <strong>N is self-contained per strategy.</strong> Curated library strategies run
            with N = 1 — each is graded on its own Sharpe, so SR₀ collapses to its N = 1 minimum
            and no multiple-testing deflation is applied. N &gt; 1 only applies to a strategy
            that comes out of a multi-candidate generation pool, where N counts every candidate
            actually tried in that round.
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Our threshold</div>
            <div className="body" style={{ fontWeight: 600 }}>confidence ≥ 0.95 → 0.50</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              The Verified badge needs ≥95% confidence the Sharpe is genuine. The strictness
              slider relaxes this down to a hard floor of 0.50 (never a coin flip) at the
              riskiest level.
            </div>
          </div>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Library example</div>
            <div className="body" style={{ fontWeight: 600, color: 'var(--positive)' }}>
              Moreira-Muir: DSR = 0.55, p = 0.995
            </div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              Graded on its own Sharpe (N = 1, undeflated) with a standard error robust to
              non-normality and serial correlation (Newey–West HAC) — the market's
              volatility-managed premium clears the bar without relying on a multiple-testing
              correction.
            </div>
          </div>
        </div>
      </div>

      {/* Primitive 2 — PBO */}
      <div className="card-elevated mb-6">
        <div className="flex items-center gap-3 mb-4">
          <div style={{
            width: 32, height: 32, borderRadius: '50%', background: 'rgba(99,102,241,0.15)',
            border: '1px solid rgba(99,102,241,0.4)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontWeight: 700, color: 'var(--accent)', flexShrink: 0,
          }}>2</div>
          <div>
            <div className="label">Probability of Backtest Overfitting (PBO)</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>Bailey, Borwein, López de Prado & Zhu (2014) — CSCV</div>
          </div>
          <span className="tag tag-positive" style={{ marginLeft: 'auto' }}>PBO &lt; 50%</span>
        </div>

        <p className="body" style={{ marginBottom: 16, lineHeight: 1.65 }}>
          If we split the historical data 16 different ways and test which half trains best,
          what is the probability our strategy underperforms its own median performance
          out-of-sample? A low PBO means the strategy's edge is consistent across time splits
          — not a single lucky run that happens to fit the training window.
        </p>

        <div className="card-flat" style={{ padding: 14, marginBottom: 12 }}>
          <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 6, textTransform: 'uppercase', fontSize: '0.68rem', letterSpacing: '0.06em' }}>
            Method (CSCV)
          </div>
          <div className="body" style={{ fontSize: '0.85rem', color: 'var(--text-2)', lineHeight: 1.6 }}>
            Divide the return series into 16 equal sub-periods. For each of the
            C(16,8) = 12,870 IS/OOS splits, pick the in-sample-best strategy and rank
            it in the OOS distribution. PBO = fraction of splits where the IS-best
            underperforms the OOS median.
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Our threshold</div>
            <div className="body" style={{ fontWeight: 600 }}>PBO &lt; 50%</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              Less likely than random to underperform median out-of-sample.
              Lower is better — 0% would mean perfect consistency.
            </div>
          </div>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Library example</div>
            <div className="body" style={{ fontWeight: 600, color: 'var(--positive)' }}>
              Both Tier-1 strategies: PBO ≈ 39%
            </div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              They outperform their own OOS median in 61% of OOS hold-out splits —
              consistent, not cherry-picked.
            </div>
          </div>
        </div>
      </div>

      {/* Primitive 3 — OOS Sharpe */}
      <div className="card-elevated mb-6">
        <div className="flex items-center gap-3 mb-4">
          <div style={{
            width: 32, height: 32, borderRadius: '50%', background: 'rgba(99,102,241,0.15)',
            border: '1px solid rgba(99,102,241,0.4)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontWeight: 700, color: 'var(--accent)', flexShrink: 0,
          }}>3</div>
          <div>
            <div className="label">Walk-Forward Out-of-Sample Sharpe</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>Chronological 70/30 train-test split</div>
          </div>
          <span className="tag tag-positive" style={{ marginLeft: 'auto' }}>OOS ≥ 50% of in-sample</span>
        </div>

        <p className="body" style={{ marginBottom: 16, lineHeight: 1.65 }}>
          Train on the first 70% of history. Test on the final 30% — the data the
          strategy never saw during optimisation. The out-of-sample Sharpe must be at
          least 50% of the full-sample Sharpe. This rules out strategies that look great
          on 20 years of data but crash on the most recent 6 years, which are always the
          most relevant for live trading. No performance cliff allowed.
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Our threshold</div>
            <div className="body" style={{ fontWeight: 600 }}>OOS ≥ 50% of in-sample Sharpe</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              Prevents strategies that look good historically but degrade on modern data.
            </div>
          </div>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Library examples</div>
            <div className="body" style={{ fontWeight: 600, color: 'var(--positive)' }}>
              Moreira-Muir: in-sample 0.77, OOS 0.97
            </div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              Performance actually <em>improves</em> out-of-sample — the premium
              has strengthened in recent years. TSMOM OOS: 0.76 vs in-sample 0.65.
            </div>
          </div>
        </div>
      </div>

      {/* Primitive 4 — Look-ahead audit */}
      <div className="card-elevated mb-6">
        <div className="flex items-center gap-3 mb-4">
          <div style={{
            width: 32, height: 32, borderRadius: '50%', background: 'rgba(99,102,241,0.15)',
            border: '1px solid rgba(99,102,241,0.4)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontWeight: 700, color: 'var(--accent)', flexShrink: 0,
          }}>4</div>
          <div>
            <div className="label">Look-Ahead Audit</div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>Static code review — binary pass/fail</div>
          </div>
          <span className="tag tag-positive" style={{ marginLeft: 'auto' }}>All pass</span>
        </div>

        <p className="body" style={{ marginBottom: 16, lineHeight: 1.65 }}>
          A static check that the backtest code never peeked at future data.
          Common look-ahead bugs include: rebalancing at the exact high or low of a period
          (impossible in real trading), using tomorrow's open price to fill today's signal,
          or normalising returns across the full history before splitting into train/test sets.
          These bugs inflate backtested Sharpe ratios dramatically and are embarrassingly common
          in published research.
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>What we check</div>
            <ul className="body" style={{ paddingLeft: 16, lineHeight: 1.7, fontSize: '0.83rem', color: 'var(--text-2)' }}>
              <li>No fills at intrabar extremes (open/close only)</li>
              <li>Signal computed on bar N, filled on bar N+1</li>
              <li>No full-history normalisation before IS/OOS split</li>
              <li>Transaction costs applied on every trade</li>
            </ul>
          </div>
          <div className="card-flat" style={{ padding: 12 }}>
            <div className="caption" style={{ color: 'var(--text-3)', marginBottom: 4 }}>Library result</div>
            <div className="body" style={{ fontWeight: 600, color: 'var(--positive)' }}>
              All 6 strategies: audit passed
            </div>
            <div className="caption" style={{ color: 'var(--text-3)' }}>
              Verified by code review of each backtrader strategy file in
              analytics-engine/strategies/. All trades fill on next-bar open.
            </div>
          </div>
        </div>
      </div>

      {/* Summary table — verdict snapshot from the canonical seed-strategy
          backtest run. The verdicts (Tier 1 vs Candidate) reflect what the
          rigor gate computes; the per-cell numbers are the snapshot values
          from the last seed run, not a live re-compute on every page load. */}
      <div className="card-flat" style={{ padding: 20, marginBottom: 8 }}>
        <div className="label mb-3">Gate Snapshot — Seed Strategy Library</div>
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th className="text-right">DSR conf</th>
                <th className="text-right">PBO</th>
                <th className="text-right">OOS Sharpe</th>
                <th className="text-right">Look-ahead</th>
                <th>Gate</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td style={{ fontWeight: 500 }}>Moreira-Muir 2017 (Vol-Managed)</td>
                <td className="text-right positive">0.995</td>
                <td className="text-right positive">39%</td>
                <td className="text-right positive">0.97</td>
                <td className="text-right positive">Pass</td>
                <td><span className="tag tag-positive" style={{ fontSize: '0.68rem' }}>Tier 1</span></td>
              </tr>
              <tr>
                <td style={{ fontWeight: 500 }}>Moskowitz-Ooi-Pedersen 2012 (TSMOM)</td>
                <td className="text-right positive">0.976</td>
                <td className="text-right positive">39%</td>
                <td className="text-right positive">0.76</td>
                <td className="text-right positive">Pass</td>
                <td><span className="tag tag-positive" style={{ fontSize: '0.68rem' }}>Tier 1</span></td>
              </tr>
              <tr>
                <td style={{ fontWeight: 500 }}>Faber 2007 (SMA-200)</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>0.612</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>37%</td>
                <td className="text-right positive">0.93</td>
                <td className="text-right positive">Pass</td>
                <td><span className="tag tag-muted" style={{ fontSize: '0.68rem' }}>Candidate</span></td>
              </tr>
              <tr>
                <td style={{ fontWeight: 500 }}>George-Hwang 2004 (52W High)</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>0.609</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>34%</td>
                <td className="text-right positive">0.91</td>
                <td className="text-right positive">Pass</td>
                <td><span className="tag tag-muted" style={{ fontSize: '0.68rem' }}>Candidate</span></td>
              </tr>
              <tr>
                <td style={{ fontWeight: 500 }}>Buy-and-Hold Baseline</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>0.891</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>39%</td>
                <td className="text-right positive">0.79</td>
                <td className="text-right positive">Pass</td>
                <td><span className="tag tag-muted" style={{ fontSize: '0.68rem' }}>Candidate</span></td>
              </tr>
              <tr>
                <td style={{ fontWeight: 500 }}>Capital Preservation (T-Bill)</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>0.812</td>
                <td className="text-right positive">24%</td>
                <td className="text-right" style={{ color: 'var(--text-3)' }}>0.43</td>
                <td className="text-right positive">Pass</td>
                <td><span className="tag tag-muted" style={{ fontSize: '0.68rem' }}>Candidate</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="caption" style={{ marginTop: 10, color: 'var(--text-3)', fontSize: '0.72rem' }}>
          Badge (Conservative/level-1) thresholds: DSR conf ≥ 0.95 · PBO &lt; 50% · OOS ≥ 50% of in-sample Sharpe.
          The strictness slider relaxes these toward the always-on floors (look-ahead pass · OOS &gt; 0 · DSR conf ≥ 0.50).
          Numbers above are a snapshot from the most recent seed backtest run; the verdict is recomputed by the
          gate at the chosen strictness every time.
        </div>
      </div>

      <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.72rem', lineHeight: 1.6 }}>
        Bailey & López de Prado (2014) "The Deflated Sharpe Ratio" ·
        Bailey, Borwein, López de Prado & Zhu (2014) "The Probability of Backtest Overfitting"
      </div>
    </div>
  )
}
