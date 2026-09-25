// x-axis tick guard for the Explore price charts (#1602).
//
// The owner report was "the x-axis dates look messed up and not accurate, hard
// to read". The root cause was a Date round-trip in PriceHistoryChart.jsx:
//
//     const d = new Date(Date.parse(ts.replace(' ', 'T')))
//     label = `${d.getMonth() + 1}-${d.getDate()}`
//
// `Date.parse` on an offset-bearing stamp yields an absolute instant, which
// `getMonth`/`getDate` then read back through the VIEWER'S LOCAL calendar. In
// America/New_York a UTC-midnight daily close "2026-05-25 00:00:00+00:00" came
// out as "05-24" — a date that appears nowhere in the series.
//
// The property under test is therefore not "the formatter formats"; it is
// **the rendered label equals the series timestamp's own calendar date, in
// every timezone**. The timezone-matrix test below is the real guard: it fails
// against the reverted implementation and passes against this one. Everything
// in this file is pure — no DOM, no network, no clock read.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
	buildXTicks,
	chooseTickCount,
	formatTickLabel,
	medianStepMs,
	parseSeriesTimestamp,
	pickTickFormat,
} from '../src/chartTicks.js'

// ── Fixtures ────────────────────────────────────────────────────────────────
//
// Shapes taken from what the backend actually emits: points are
// `ExploreHistoryPoint(ts=str(pandas.Timestamp), price=float)`
// (asset_market_service._fetch_yfinance_series), so `ts` is whatever a pandas
// Timestamp stringifies to — tz-aware UTC for crypto, tz-aware exchange-local
// for US equities, and tz-naive/date-only for resampled weekly and monthly
// bars.

/** 10 daily crypto closes, UTC-stamped. The exact shape that mislabelled. */
const DAILY_UTC = [
	{ ts: '2026-05-18 00:00:00+00:00', price: 100 },
	{ ts: '2026-05-19 00:00:00+00:00', price: 101 },
	{ ts: '2026-05-20 00:00:00+00:00', price: 102 },
	{ ts: '2026-05-21 00:00:00+00:00', price: 103 },
	{ ts: '2026-05-22 00:00:00+00:00', price: 104 },
	{ ts: '2026-05-23 00:00:00+00:00', price: 105 },
	{ ts: '2026-05-24 00:00:00+00:00', price: 106 },
	{ ts: '2026-05-25 00:00:00+00:00', price: 107 },
	{ ts: '2026-05-26 00:00:00+00:00', price: 108 },
	{ ts: '2026-05-27 00:00:00+00:00', price: 109 },
]

/** 5 daily US-equity bars, stamped in exchange-local time (yfinance's default
 * for an equity ticker). */
const DAILY_EQUITY_ET = [
	{ ts: '2026-05-18 00:00:00-04:00', price: 200 },
	{ ts: '2026-05-19 00:00:00-04:00', price: 201 },
	{ ts: '2026-05-20 00:00:00-04:00', price: 202 },
	{ ts: '2026-05-21 00:00:00-04:00', price: 203 },
	{ ts: '2026-05-22 00:00:00-04:00', price: 204 },
]

/** Intraday 5-minute bars inside one session. */
const INTRADAY_ONE_DAY = [
	{ ts: '2026-05-25 09:30:00-04:00', price: 10 },
	{ ts: '2026-05-25 09:35:00-04:00', price: 11 },
	{ ts: '2026-05-25 09:40:00-04:00', price: 12 },
	{ ts: '2026-05-25 09:45:00-04:00', price: 13 },
	{ ts: '2026-05-25 09:50:00-04:00', price: 14 },
]

/** Intraday bars spanning two calendar days — what the 1D range actually
 * returns, because it is served as period "2d". */
const INTRADAY_TWO_DAYS = [
	{ ts: '2026-05-25 09:30:00-04:00', price: 10 },
	{ ts: '2026-05-25 09:35:00-04:00', price: 11 },
	{ ts: '2026-05-25 09:40:00-04:00', price: 12 },
	{ ts: '2026-05-26 09:30:00-04:00', price: 13 },
	{ ts: '2026-05-26 09:35:00-04:00', price: 14 },
	{ ts: '2026-05-26 09:40:00-04:00', price: 15 },
]

/** Monthly bars over 10 years (the MAX / 10Y ranges). */
const MONTHLY_DECADE = Array.from({ length: 121 }, (_, i) => {
	const year = 2016 + Math.floor(i / 12)
	const month = (i % 12) + 1
	return { ts: `${year}-${String(month).padStart(2, '0')}-01 00:00:00`, price: 100 + i }
})

/** A short window that straddles New Year — "Jan 2" and "Dec 30" alone would
 * not say which year each belongs to. */
const STRADDLES_NEW_YEAR = [
	{ ts: '2025-12-29', price: 50 },
	{ ts: '2025-12-30', price: 51 },
	{ ts: '2025-12-31', price: 52 },
	{ ts: '2026-01-02', price: 53 },
	{ ts: '2026-01-05', price: 54 },
]

// Plot widths: the desktop chart is a 720px container less the 56px y-gutter
// and 18px right pad; the phone case is a 360px container less 44px and 18px.
const DESKTOP_PLOT_W = 720 - 56 - 18 // 646
const MOBILE_PLOT_W = 360 - 44 - 18 // 298

// ── The regression guard: labels must not move with the viewer's timezone ───

/** Run `fn` with process.env.TZ set, restoring it afterwards. Node reads TZ
 * per Date construction, so this genuinely re-homes the local calendar. */
function withTimezone(tz, fn) {
	const prev = process.env.TZ
	process.env.TZ = tz
	try {
		return fn()
	} finally {
		if (prev === undefined) delete process.env.TZ
		else process.env.TZ = prev
	}
}

// UTC-14 through UTC+14 in practice: one zone well west of UTC (where the old
// code shifted dates backwards), UTC itself (where the old code was correct,
// which is why this shipped), and one well east.
const TIMEZONES = ['America/Los_Angeles', 'America/New_York', 'UTC', 'Europe/Berlin', 'Asia/Tokyo', 'Pacific/Kiritimati']

test('#1602: a UTC-stamped daily close labels its OWN date in every timezone', () => {
	// This is the exact reported defect. The last bar in the fixture is
	// 2026-05-27; west of UTC the old implementation rendered "05-26".
	for (const tz of TIMEZONES) {
		withTimezone(tz, () => {
			const ticks = buildXTicks(DAILY_UTC, { plotWidth: DESKTOP_PLOT_W })
			assert.deepEqual(
				ticks.map(t => t.label),
				['May 18', 'May 20', 'May 22', 'May 23', 'May 25', 'May 27'],
				`x-axis labels drifted in ${tz}`,
			)
		})
	}
})

test('#1602: every tick label equals the calendar date of the sample it marks', () => {
	// Stronger than the fixed-list assertion above: whatever ticks are chosen,
	// each label must be derivable from that sample's own timestamp text. This
	// is the "no index-as-label, no off-by-one" property stated directly.
	for (const tz of TIMEZONES) {
		withTimezone(tz, () => {
			for (const series of [DAILY_UTC, DAILY_EQUITY_ET, MONTHLY_DECADE, INTRADAY_TWO_DAYS]) {
				const fmt = pickTickFormat(series)
				for (const tick of buildXTicks(series, { plotWidth: DESKTOP_PLOT_W })) {
					const ts = series[tick.index].ts
					assert.equal(tick.label, formatTickLabel(ts, fmt))
					// And the label's date parts really are that timestamp's date parts.
					const f = parseSeriesTimestamp(ts)
					if (fmt === 'day' || fmt === 'dayYear' || fmt === 'dayTime') {
						assert.match(tick.label, new RegExp(`\\b${f.day}\\b`), `${tick.label} vs ${ts} in ${tz}`)
					}
					if (fmt === 'dayYear' || fmt === 'month' || fmt === 'year') {
						assert.match(tick.label, new RegExp(`\\b${f.year}\\b`), `${tick.label} vs ${ts} in ${tz}`)
					}
				}
			}
		})
	}
})

test('#1602: a date-only stamp is not shifted either (Date.parse treats it as UTC)', () => {
	// "2026-05-25" parses as UTC midnight per ECMA-262, so local getters moved
	// it back a day west of UTC — the same bug through a different door.
	for (const tz of TIMEZONES) {
		withTimezone(tz, () => {
			assert.equal(formatTickLabel('2026-05-25', 'day'), 'May 25')
			assert.equal(formatTickLabel('2026-05-25', 'dayYear'), 'May 25, 2026')
			assert.equal(formatTickLabel('2026-05-25', 'year'), '2026')
		})
	}
})

test('#1602: an intraday bar keeps the exchange clock it was stamped with', () => {
	// A 09:30 ET open must read 09:30 on the axis, not 06:30 because the viewer
	// is in Los Angeles or 22:30 because they are in Tokyo.
	for (const tz of TIMEZONES) {
		withTimezone(tz, () => {
			const ticks = buildXTicks(INTRADAY_ONE_DAY, { plotWidth: DESKTOP_PLOT_W })
			assert.equal(ticks[0].label, '09:30', `intraday open drifted in ${tz}`)
			assert.equal(ticks[ticks.length - 1].label, '09:50', `intraday close drifted in ${tz}`)
		})
	}
})

// ── Lexical parsing ─────────────────────────────────────────────────────────

test('parseSeriesTimestamp reads each pandas stringification the backend emits', () => {
	assert.deepEqual(parseSeriesTimestamp('2026-05-25'), { year: 2026, month: 5, day: 25, hour: 0, minute: 0 })
	assert.deepEqual(parseSeriesTimestamp('2026-05-25 00:00:00'), { year: 2026, month: 5, day: 25, hour: 0, minute: 0 })
	assert.deepEqual(parseSeriesTimestamp('2026-05-25 09:35:00+00:00'), { year: 2026, month: 5, day: 25, hour: 9, minute: 35 })
	assert.deepEqual(parseSeriesTimestamp('2026-05-25T09:35:00-04:00'), { year: 2026, month: 5, day: 25, hour: 9, minute: 35 })
})

test('parseSeriesTimestamp rejects junk instead of inventing a date', () => {
	for (const bad of [null, undefined, 42, '', 'not-a-date', '2026-13-01', '2026-05-32', '2026-05-25 25:00', 'May 25 2026']) {
		assert.equal(parseSeriesTimestamp(bad), null, `should reject ${JSON.stringify(bad)}`)
	}
})

test('formatTickLabel falls back to the raw stamp rather than NaN for junk', () => {
	// The old code produced "NaN-NaN" here; an honest fallback shows what came in.
	assert.equal(formatTickLabel('not-a-date', 'day'), 'not-a-date')
	assert.equal(formatTickLabel(undefined, 'day'), '')
	assert.doesNotMatch(formatTickLabel('not-a-date', 'day'), /NaN/)
})

// ── Format selection ────────────────────────────────────────────────────────

test('medianStepMs is robust to one gap (the old first-pair heuristic was not)', () => {
	// A weekend or a halted session sits between the first two bars. The old
	// `isIntraday` looked only at points[0] and points[1], so one leading gap
	// flipped an entire intraday axis to date labels.
	const gapFirst = [
		{ ts: '2026-05-22 15:55:00-04:00' },
		{ ts: '2026-05-25 09:30:00-04:00' }, // 3-day weekend gap
		{ ts: '2026-05-25 09:35:00-04:00' },
		{ ts: '2026-05-25 09:40:00-04:00' },
		{ ts: '2026-05-25 09:45:00-04:00' },
	].map(p => parseSeriesTimestamp(p.ts))
	assert.equal(medianStepMs(gapFirst), 5 * 60 * 1000)
	assert.equal(medianStepMs([]), null)
	assert.equal(medianStepMs([parseSeriesTimestamp('2026-05-25')]), null)
})

test('pickTickFormat picks resolution from the bar interval and span', () => {
	assert.equal(pickTickFormat(INTRADAY_ONE_DAY), 'time')
	assert.equal(pickTickFormat(INTRADAY_TWO_DAYS), 'dayTime')
	assert.equal(pickTickFormat(DAILY_UTC), 'day')
	assert.equal(pickTickFormat(DAILY_EQUITY_ET), 'day')
	assert.equal(pickTickFormat(STRADDLES_NEW_YEAR), 'dayYear')
	assert.equal(pickTickFormat(MONTHLY_DECADE), 'year')
	assert.equal(pickTickFormat([]), 'day')
})

test('a 1D chart spanning two sessions does not repeat the same clock times', () => {
	// The 1D range is served as period "2d", so bare HH:MM labels read
	// "09:30 … 09:30" across the axis with nothing to tell the days apart.
	const labels = buildXTicks(INTRADAY_TWO_DAYS, { plotWidth: DESKTOP_PLOT_W }).map(t => t.label)
	assert.deepEqual(labels, [
		'May 25 09:30', 'May 25 09:35', 'May 25 09:40',
		'May 26 09:30', 'May 26 09:35', 'May 26 09:40',
	])
	assert.equal(new Set(labels).size, labels.length, 'labels must be distinguishable')
})

test('a decade chart labels years, not bare month-days', () => {
	// The reported "hard to read": 10 years of "05-25"-style labels carry no
	// year at all, so every tick looks like it belongs to the same month.
	const labels = buildXTicks(MONTHLY_DECADE, { plotWidth: DESKTOP_PLOT_W }).map(t => t.label)
	assert.deepEqual(labels, ['2016', '2018', '2020', '2022', '2024', '2026'])
})

test('a New-Year-straddling window carries the year on every label', () => {
	const labels = buildXTicks(STRADDLES_NEW_YEAR, { plotWidth: DESKTOP_PLOT_W }).map(t => t.label)
	assert.deepEqual(labels, ['Dec 29, 2025', 'Dec 30, 2025', 'Dec 31, 2025', 'Jan 2, 2026', 'Jan 5, 2026'])
	// The point of the format: "Dec 31" and "Jan 2" alone would not say which
	// side of the year boundary each tick sits on.
	assert.ok(labels.every(l => /\b20\d\d$/.test(l)))
})

// ── Tick density ────────────────────────────────────────────────────────────

test('chooseTickCount never lets two labels touch, at any width', () => {
	// The property: with n ticks the spacing is plotWidth/(n-1), which must
	// leave a real gap after the widest label that format can produce.
	const CHAR_W = 11 * 0.62
	const WIDEST = { time: 5, dayTime: 12, day: 6, dayYear: 12, month: 8, year: 4 }
	for (const fmt of Object.keys(WIDEST)) {
		for (const w of [120, 180, 240, 298, 400, 520, 646, 900, 1400]) {
			const n = chooseTickCount(w, fmt)
			assert.ok(n >= 2 && n <= 6, `${fmt}@${w}: ${n} outside [2,6]`)
			if (n > 2) {
				const spacing = w / (n - 1)
				assert.ok(
					spacing >= WIDEST[fmt] * CHAR_W,
					`${fmt}@${w}: ${n} ticks gives ${spacing.toFixed(1)}px spacing for a ${(WIDEST[fmt] * CHAR_W).toFixed(1)}px label`,
				)
			}
		}
	}
})

test('tick density actually adapts: a phone gets fewer ticks than a desktop', () => {
	// Guards the whole point of measuring the container. If the viewBox were
	// still a constant 720, these two would be identical and this fails.
	const desktop = buildXTicks(MONTHLY_DECADE, { plotWidth: DESKTOP_PLOT_W, format: 'dayYear' })
	const mobile = buildXTicks(MONTHLY_DECADE, { plotWidth: MOBILE_PLOT_W, format: 'dayYear' })
	assert.equal(desktop.length, 6)
	assert.equal(mobile.length, 4)
	assert.ok(mobile.length < desktop.length)
	// And the narrower axis still spans the whole series.
	assert.equal(mobile[0].index, 0)
	assert.equal(mobile[mobile.length - 1].index, MONTHLY_DECADE.length - 1)
})

test('chooseTickCount degrades safely on a nonsense width', () => {
	for (const w of [0, -50, NaN, undefined, Infinity]) {
		assert.equal(chooseTickCount(w, 'day'), 2, `width ${w} should fall back to 2 ticks`)
	}
})

// ── Tick placement ──────────────────────────────────────────────────────────

test('buildXTicks always marks the first and last sample', () => {
	// These are the two values a reader actually looks up; an axis that omits
	// the endpoints of the series it is drawing is lying about the range.
	for (const series of [DAILY_UTC, DAILY_EQUITY_ET, MONTHLY_DECADE, INTRADAY_TWO_DAYS, STRADDLES_NEW_YEAR]) {
		const ticks = buildXTicks(series, { plotWidth: DESKTOP_PLOT_W })
		assert.equal(ticks[0].index, 0)
		assert.equal(ticks[ticks.length - 1].index, series.length - 1)
	}
})

test('buildXTicks anchors the edge labels inward so neither is clipped', () => {
	// Centring the first label hung it into the y-axis gutter; centring the
	// last pushed it past the right edge of the viewBox, where it was cut off.
	const ticks = buildXTicks(DAILY_UTC, { plotWidth: DESKTOP_PLOT_W })
	assert.equal(ticks[0].anchor, 'start')
	assert.equal(ticks[ticks.length - 1].anchor, 'end')
	for (const t of ticks.slice(1, -1)) assert.equal(t.anchor, 'middle')
})

test('buildXTicks indices are strictly increasing and inside the series', () => {
	for (const series of [DAILY_UTC, MONTHLY_DECADE, INTRADAY_TWO_DAYS]) {
		const idx = buildXTicks(series, { plotWidth: DESKTOP_PLOT_W }).map(t => t.index)
		for (let i = 1; i < idx.length; i += 1) assert.ok(idx[i] > idx[i - 1], `not increasing: ${idx}`)
		assert.ok(idx.every(i => i >= 0 && i < series.length))
	}
})

test('buildXTicks handles degenerate series without duplicating a tick', () => {
	assert.deepEqual(buildXTicks([], { plotWidth: DESKTOP_PLOT_W }), [])
	assert.deepEqual(buildXTicks(null, { plotWidth: DESKTOP_PLOT_W }), [])

	const one = buildXTicks([{ ts: '2026-05-25', price: 1 }], { plotWidth: DESKTOP_PLOT_W })
	assert.equal(one.length, 1)
	assert.equal(one[0].label, 'May 25')
	assert.equal(one[0].anchor, 'start')

	const two = buildXTicks([{ ts: '2026-05-25', price: 1 }, { ts: '2026-05-26', price: 2 }], { plotWidth: DESKTOP_PLOT_W })
	assert.deepEqual(two.map(t => t.index), [0, 1])
	assert.deepEqual(two.map(t => t.label), ['May 25', 'May 26'])
})
