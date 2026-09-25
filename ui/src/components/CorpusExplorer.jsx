import { useState, useEffect, useCallback } from 'react'
import CustomSelect from './CustomSelect'
import CorpusGraph from './CorpusGraph'
import CorpusKG from './CorpusKG'
import { cleanLatex } from '../utils/latex'
import { apiGet } from '../api'
import { runCatalogFetch, scheduleCatalogFetch } from '../corpusSearch'
import { KNOWLEDGE_GRAPH_TAB_ENABLED } from '../featureFlags'

// The Topic Clusters tab is filtered out at build time unless the flag is on
// (#1406). Its backing tables (kg_entities/kg_relations) are 0 rows until
// #1090 produces the KB pipeline artifact and #1092 backfills Postgres, so
// shipping the tab means a visitor has to click an empty capability to find
// that out. #1392 made the zero-state itself honest; this stops the round
// trip. The tab returns by flipping the flag, not by editing this array.
const TABS = ['catalog', 'overview', 'graph', ...(KNOWLEDGE_GRAPH_TAB_ENABLED ? ['knowledge-graph'] : [])]

const TAB_LABELS = {
  catalog: 'Catalog',
  overview: 'Overview',
  graph: 'Graph',
  // Renamed from the old bare capability-name label: the backing table is 0
  // rows (kg_entities/kg_relations), and CorpusKG.jsx's own docstring/loading
  // copy already describe this view as topic clusters — the tab label was the
  // one place on this surface still overclaiming (#1368).
  'knowledge-graph': 'Topic Clusters',
}

export default function CorpusExplorer() {
  const [tab, setTab] = useState('catalog')
  const [overview, setOverview] = useState(null)
  // Overview fetch failure (#1356): a null overview used to be permanently
  // indistinguishable from "still loading" — OverviewTab rendered "Loading
  // overview..." forever, with no error and no retry.
  const [overviewError, setOverviewError] = useState(false)
  const [papers, setPapers] = useState([])
  const [selectedPaper, setSelectedPaper] = useState(null)
  const [search, setSearch] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('')
  const [page, setPage] = useState(1)
  const [totalPapers, setTotalPapers] = useState(0)
  const [loading, setLoading] = useState(false)
  // Catalog fetch failure (#1356): totalPapers stayed at its initial 0 on a
  // failed fetch, and CatalogTab announced that 0 as "0 papers found" inside
  // a role="status" live region — an outage read to a screen reader as a
  // measured result.
  const [catalogError, setCatalogError] = useState(false)

  // Fetch overview
  const fetchOverview = useCallback(() => {
    setOverviewError(false)
    return apiGet('/api/corpus/overview')
      .then(setOverview)
      .catch(() => setOverviewError(true))
  }, [])

  useEffect(() => { fetchOverview() }, [fetchOverview])

  // Fetch papers catalog.
  //
  // `signal` is optional so the Retry button can still call this directly; the
  // debounced path always supplies one. See ../corpusSearch.js for why the
  // aborted-after-resolve check has to happen after every await.
  const fetchPapers = useCallback(async (signal) => {
    setLoading(true)
    setCatalogError(false)
    const params = new URLSearchParams({ page: String(page), limit: '20' })
    if (search) params.set('search', search)
    if (categoryFilter) params.set('category', categoryFilter)
    const outcome = await runCatalogFetch(signal, {
      // Trailing slash matters: FastAPI 307-redirects /api/papers → /api/papers/,
      // and the browser silently drops the query string on the redirect, so the
      // catalog rendered "0 papers found" despite the backend having 10000.
      fetchPage: s => apiGet(`/api/papers/?${params}`, { signal: s }),
      onResult: data => {
        setPapers(data.papers || [])
        setTotalPapers(data.total || 0)
      },
      onError: () => {
        setPapers([])
        setCatalogError(true)
      },
    })
    // A superseded request leaves `loading` alone on purpose: the request that
    // replaced it is still in flight and already set it, so clearing it here
    // would flash "not loading" mid-type. The successor owns the reset.
    if (outcome !== 'superseded') setLoading(false)
  }, [page, search, categoryFilter])

  // One AbortController and one 300 ms timer per render of this effect; the
  // cleanup cancels both. Eight keystrokes therefore produce seven cancels and
  // one request, instead of eight requests racing each other (#1665). Page and
  // category changes take the same path — they are single clicks, so the 300 ms
  // is invisible, and routing every catalog fetch through one scheduler is what
  // makes "the newest request always wins" true rather than nearly true.
  useEffect(() => scheduleCatalogFetch(fetchPapers), [fetchPapers])

  // Wrapped, not passed bare as `onRetry={fetchPapers}`: as an onClick handler
  // it would receive the click event in its `signal` parameter, and that event
  // would then be handed to `fetch` as an AbortSignal. Retry is a deliberate
  // click, so it runs immediately and without a controller rather than through
  // the debounce.
  const retryPapers = useCallback(() => { fetchPapers() }, [fetchPapers])

  // Fetch paper detail
  const openPaper = async (arxivId) => {
    try {
      const data = await apiGet(`/api/papers/${arxivId}`)
      setSelectedPaper(data)
    } catch { setSelectedPaper(null) }
  }

  if (selectedPaper) {
    return <PaperDetail paper={selectedPaper} onBack={() => setSelectedPaper(null)} />
  }

  return (
    <div className="corpus-explorer">
      <div className="corpus-header">
        <h2>Research Corpus Explorer</h2>
        {/* An overview fetch failure must stay visible on every tab, not just
            the Overview tab — this header renders regardless of which tab is
            active (default 'catalog'), so silently falling through to
            `overview && (...)` here would make the outage invisible on the
            tab a visitor lands on first (#1356). */}
        {overviewError ? (
          <div className="corpus-stats">
            <span className="stat-chip">corpus stats unavailable</span>
          </div>
        ) : overview && (
          <div className="corpus-stats">
            {/* Qualified, not bare: the count is real (paper_count in prod), but
                unqualified "papers" reads as fully-processed corpus when the KB
                pipeline has processed 0 of them (#778 item A / #1368). */}
            <span className="stat-chip">{overview.total_papers?.toLocaleString()} paper metadata records</span>
            <span className="stat-chip">{overview.categories?.length} categories</span>
            <span className="stat-chip">{overview.source} source</span>
          </div>
        )}
      </div>

      <div className="corpus-tabs">
        {TABS.map(t => (
          <button key={t} className={`corpus-tab${tab === t ? ' active' : ''}`} onClick={() => setTab(t)}>
            {TAB_LABELS[t] ?? t.replace('-', ' ').replace(/\b\w/g, c => c.toUpperCase())}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <OverviewTab overview={overview} overviewError={overviewError} onRetry={fetchOverview} />
      )}
      {tab === 'catalog' && (
        <CatalogTab
          papers={papers} total={totalPapers} page={page} loading={loading}
          catalogError={catalogError} onRetry={retryPapers}
          search={search} setSearch={setSearch}
          categoryFilter={categoryFilter} setCategoryFilter={setCategoryFilter}
          setPage={setPage} openPaper={openPaper}
          categories={overview?.categories || []}
        />
      )}
      {tab === 'graph' && (
        <div className="corpus-graph-container" style={{ padding: '8px 0' }}>
          <CorpusGraph />
        </div>
      )}
      {tab === 'knowledge-graph' && (
        <div style={{ padding: '8px 0' }}>
          <CorpusKG />
        </div>
      )}
    </div>
  )
}

function OverviewTab({ overview, overviewError, onRetry }) {
  if (overviewError) {
    return (
      <div className="corpus-loading">
        Overview unavailable — the corpus overview request failed.{' '}
        <button type="button" className="btn btn-sm btn-outline" onClick={onRetry} style={{ marginLeft: 4 }}>
          Retry
        </button>
      </div>
    )
  }
  if (!overview) return <div className="corpus-loading">Loading overview...</div>

  const maxCatCount = Math.max(...(overview.categories || []).map(c => c.count), 1)
  const maxYearCount = Math.max(...(overview.year_distribution || []).map(y => y.count), 1)

  return (
    <div className="corpus-overview">
      <div className="overview-section">
        <h3>Category Distribution</h3>
        <div className="bar-chart">
          {(overview.categories || []).map(c => (
            <div key={c.name} className="bar-row">
              <span className="bar-label" title={c.name}>{c.label || c.name}</span>
              <div className="bar-track">
                <div className="bar-fill" style={{ width: `${(c.count / maxCatCount) * 100}%` }} />
              </div>
              <span className="bar-count">{c.count.toLocaleString()}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="overview-section">
        <h3>Year Distribution</h3>
        <div className="year-chart">
          {(overview.year_distribution || []).map(y => (
            <div key={y.year} className="year-bar-row">
              <span className="year-label">{y.year}</span>
              <div className="year-track">
                <div className="year-fill" style={{ width: `${(y.count / maxYearCount) * 100}%` }} />
              </div>
              <span className="year-count">{y.count.toLocaleString()}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="overview-summary">
        <h3>Library Summary</h3>
        <div className="summary-grid">
          <div className="summary-card">
            <div className="summary-value">{overview.total_papers?.toLocaleString()}</div>
            <div className="summary-label">Total Papers</div>
          </div>
          <div className="summary-card">
            <div className="summary-value">{overview.categories?.length}</div>
            <div className="summary-label">Categories</div>
          </div>
          <div className="summary-card">
            <div className="summary-value">{overview.year_distribution?.length}</div>
            <div className="summary-label">Year Span</div>
          </div>
        </div>
      </div>
    </div>
  )
}

function formatAuthors(authors) {
  if (!Array.isArray(authors) || authors.length === 0) return '—'
  if (authors.length === 1) return authors[0]
  if (authors.length === 2) return `${authors[0]} & ${authors[1]}`
  return `${authors[0]}, ${authors[1]} et al.`
}

function CatalogTab({ papers, total, page, loading, catalogError, onRetry, search, setSearch, categoryFilter, setCategoryFilter, setPage, openPaper, categories }) {
  const totalPages = Math.ceil(total / 20)
  return (
    <div className="corpus-catalog">
      <div className="catalog-controls">
        {/* The placeholder is not a label: it disappears on the first keystroke
            and the field is then anonymous to a screen reader (3.3.2). */}
        <input
          type="text" placeholder="Search title, abstract, authors..." value={search}
          aria-label="Search papers by title, abstract or author name"
          aria-describedby="catalog-search-scope"
          onChange={e => { setSearch(e.target.value); setPage(1) }}
          className="catalog-search"
        />
        <CustomSelect
          value={categoryFilter}
          onChange={v => { setCategoryFilter(v); setPage(1) }}
          className="catalog-filter"
          ariaLabel="Filter papers by category"
          options={[{ value: '', label: 'All Categories' }, ...categories.map(c => ({ value: c.name, label: `${c.label || c.name} (${c.count})` }))]}
        />
      </div>

      {/* State the scope of the search, because the box cannot be judged
          without it: a user who searches a surname and sees 4 rows has no way
          to tell whether the corpus holds 4 or the author leg was never
          consulted (#1451). Wording is deliberately literal — this is a
          case-insensitive substring match over three text columns. There are
          no embeddings behind the catalog, so nothing here may read as
          semantic, ranked, or "smart". */}
      <p id="catalog-search-scope" className="catalog-search-scope">
        Substring match over title, abstract and author names — case-insensitive, no ranking.
        Author names are matched from 3 characters up.
      </p>

      {loading ? <div className="corpus-loading">Loading...</div> : catalogError ? (
        <div className="catalog-results-info" role="status">
          Catalog unavailable — the papers request failed.{' '}
          <button type="button" className="btn btn-sm btn-outline" onClick={onRetry} style={{ marginLeft: 4 }}>
            Retry
          </button>
        </div>
      ) : (
        <>
          <div className="catalog-results-info" role="status">{total.toLocaleString()} papers found</div>
          <div className="overflow-x-auto rounded-lg border border-[var(--glass-border)]">
            {/* Tight one-line-per-entry table. arxiv ID dropped from the
                listing (it shows on the paper detail page after click);
                titles get the freed horizontal space and truncate with
                ellipsis instead of wrapping. */}
            <table
              className="lib-table"
              style={{ minWidth: 560, borderCollapse: 'collapse', fontSize: '0.82rem' }}
            >
              <thead>
                <tr style={{ background: 'var(--glass)', textAlign: 'left', borderBottom: '1px solid var(--glass-border)' }}>
                  <th style={{ padding: '7px 12px', whiteSpace: 'nowrap' }}>Authors</th>
                  <th style={{ padding: '7px 12px', textAlign: 'right', whiteSpace: 'nowrap' }}>Year</th>
                  <th style={{ padding: '7px 12px' }}>Title</th>
                  <th style={{ padding: '7px 12px', whiteSpace: 'nowrap' }}>Category</th>
                </tr>
              </thead>
              <tbody>
                {papers.map(p => (
                  <tr
                    key={p.arxiv_id}
                    onClick={() => openPaper(p.arxiv_id)}
                    style={{ borderBottom: '1px solid var(--glass-border)', cursor: 'pointer' }}
                    className="hover:bg-[var(--glass)]"
                  >
                    <td
                      style={{ padding: '5px 12px', color: 'var(--text-2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                      title={Array.isArray(p.authors) ? p.authors.join(', ') : ''}
                    >
                      {formatAuthors(p.authors)}
                    </td>
                    <td style={{ padding: '5px 12px', textAlign: 'right', fontFamily: 'var(--mono, monospace)', color: 'var(--text-3)', whiteSpace: 'nowrap' }}>
                      {p.published ? p.published.slice(0, 4) : '—'}
                    </td>
                    <td
                      style={{ padding: '5px 12px', color: 'var(--text-1)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                      title={cleanLatex(p.title) || p.arxiv_id}
                    >
                      {/* Real control in the title cell: the row onClick was the
                          only way into a paper's detail view and no cell held a
                          link, so on this anonymous-OK front door a keyboard or
                          switch user could page the catalog but never open a
                          paper (2.1.1). The row keeps its onClick for the mouse;
                          stopPropagation keeps that one activation. */}
                      <button
                        type="button"
                        className="catalog-title-btn"
                        onClick={(e) => { e.stopPropagation(); openPaper(p.arxiv_id) }}
                      >
                        {cleanLatex(p.title) || p.arxiv_id}
                      </button>
                    </td>
                    <td style={{ padding: '5px 12px', whiteSpace: 'nowrap' }}>
                      {p.primary_category && (
                        <span
                          className="tag tag-muted"
                          title={p.category_label ? `${p.primary_category} — ${p.category_label}` : p.primary_category}
                          style={{ fontSize: '0.72rem' }}
                        >
                          {p.category_label || p.primary_category}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {totalPages > 1 && (
            <div className="catalog-pagination">
              <button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>Previous</button>
              <span>Page {page} of {totalPages}</span>
              <button disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>Next</button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function PaperDetail({ paper, onBack }) {
  // Always derive an arxiv URL from the id — backend doesn't always populate
  // pdf_url, but arxiv.org/abs/{id} is canonical and always works.
  const arxivAbsUrl = paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null
  // Only trust a server-supplied pdf_url if it's an https:// URL — a polluted
  // corpus record could otherwise inject a javascript:/phishing href into this
  // button. Fall back to the canonical arxiv template. (audit 2026-06-14)
  const safePdfUrl =
    typeof paper.pdf_url === 'string' && /^https:\/\//i.test(paper.pdf_url) ? paper.pdf_url : null
  const arxivPdfUrl = safePdfUrl || (paper.arxiv_id ? `https://arxiv.org/pdf/${paper.arxiv_id}` : null)

  return (
    <div className="corpus-explorer">
      <button className="back-btn flex items-center gap-1.5" onClick={onBack}><span className="i-lucide-arrow-left w-4 h-4" /> Back to Explorer</button>
      <div className="paper-detail" style={{ maxWidth: 820 }}>
        <h2 className="leading-snug mb-2">{cleanLatex(paper.title) || paper.arxiv_id}</h2>

        {paper.authors?.length > 0 && (
          <div className="caption mb-3 text-[0.92rem]">
            {paper.authors.join(', ')}
          </div>
        )}

        <div className="paper-detail-meta flex flex-wrap gap-2 mb-3.5">
          <span className="tag tag-muted mono">arxiv:{paper.arxiv_id}</span>
          {paper.primary_category && (
            <span
              className="tag tag-muted"
              title={paper.category_label ? `${paper.primary_category} — ${paper.category_label}` : paper.primary_category}
            >
              {paper.category_label || paper.primary_category}
            </span>
          )}
          {paper.published && <span className="tag tag-muted">{(paper.published || '').slice(0, 10)}</span>}
          {paper.topic_label && <span className="tag tag-accent">Topic: {paper.topic_label}</span>}
        </div>

        <div className="flex gap-2.5 mb-5 flex-wrap">
          {arxivAbsUrl && (
            <a
              href={arxivAbsUrl} target="_blank" rel="noopener noreferrer"
              className="btn btn-primary"
              style={{ padding: '6px 14px', fontSize: '0.85rem' }}
            >
              arxiv.org abstract ↗
            </a>
          )}
          {arxivPdfUrl && (
            <a
              href={arxivPdfUrl} target="_blank" rel="noopener noreferrer"
              className="btn btn-outline"
              style={{ padding: '6px 14px', fontSize: '0.85rem' }}
            >
              PDF ↗
            </a>
          )}
        </div>

        {paper.abstract && (
          <div className="paper-abstract-full" style={{ marginBottom: 24 }}>
            <h4 style={{ marginBottom: 8, fontSize: '0.9rem', color: 'var(--text-3)', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Abstract</h4>
            <p style={{ lineHeight: 1.7, fontSize: '0.95rem', color: 'var(--text-2)' }}>{cleanLatex(paper.abstract)}</p>
          </div>
        )}

        <div className="paper-provenance" style={{ borderTop: '1px solid var(--glass-border)', paddingTop: 18, marginTop: 18 }}>
          <h4 style={{ marginBottom: 10, fontSize: '0.9rem', color: 'var(--text-3)', letterSpacing: '0.05em', textTransform: 'uppercase' }}>
            Cited by {paper.citing_strategies?.length || 0} strateg{paper.citing_strategies?.length === 1 ? 'y' : 'ies'}
          </h4>
          {paper.citing_strategies?.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {paper.citing_strategies.map(s => (
                <div key={s.id || s.name} className="card" style={{ padding: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: '0.92rem' }}>{s.name || s.id}</div>
                    {s.method && (
                      <div className="caption" style={{ marginTop: 2 }}>
                        via <span className="tag tag-muted" style={{ marginLeft: 4 }}>{s.method}</span>
                        {s.status && <span style={{ marginLeft: 6 }}>· {s.status}</span>}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="caption" style={{ color: 'var(--text-4)' }}>
              No strategies in the library currently cite this paper. Generate one from{' '}
              <a href="/app/generate" style={{ color: 'var(--accent)' }}>Generate</a> — when the
              fusion engine selects this paper, the link will appear here.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
