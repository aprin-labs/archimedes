/** Where a strategy row's headline numbers actually came from.
 *
 * `GET /api/strategies/` already serves this per row as
 * `display_metrics_source` — the link of the backend's
 * `s.real_* -> bt.* -> s.stub_*` display chain that supplied the numbers
 * (`services/curated_metrics.py::display_metrics_source`). The UI dropped the
 * field, so a value carried on the strategy record and a value produced by a
 * persisted backtest run rendered as the same black number in the same cell.
 *
 * That is the half of the Examples-tab problem the copy alone cannot fix: the
 * curated examples are reference implementations, and the metrics beside them
 * are values their records ship with. Saying so next to the number is the
 * honest version of the sentence the tab's intro now makes.
 *
 * ALLOW-LIST, deliberately. Only the sources that are NOT a run made here get a
 * mark; everything else — `persisted_backtest`, `unavailable`, an older API
 * response with no such field at all — maps to `null` and the row says nothing.
 * The two omissions are load-bearing:
 *
 *   - `persisted_backtest` is a real persisted backtest row. Marking it
 *     "fixture" would be a fresh false claim, which is worse than the silence
 *     this change is fixing.
 *   - an absent/unknown value is not evidence of anything, so it earns no
 *     label in either direction (a generated row reaches the table through
 *     `coerceGenerated`, which does not carry the field).
 */

export const METRICS_SOURCE_NOTES = {
	// `display_metrics_source` returns this when `s.real_sharpe` is populated,
	// and its own comment is explicit that this is NOT "measured": for the
	// curated library those columns trace to the migrated backtest-fixture
	// snapshot (`backend/tests/fixtures/backtest_fixtures_snapshot.json`,
	// #1187), which predates the current DSR convention and gate threshold.
	strategy_record: {
		label: "fixture",
		title:
			"Fixture value — served from what the strategy record stores. For the " +
			"curated example library these numbers trace to the backtest-fixture " +
			"snapshot (#1187), not to a backtest run for this card.",
	},
	// The last link of the chain: the `BACKTEST_*` constants declared in the
	// strategy module itself.
	stub_placeholder: {
		label: "placeholder",
		title:
			"Placeholder value — the BACKTEST_* constant declared in the strategy " +
			"module. No backtest produced this number.",
	},
};

/** `{label, title}` for a metric-provenance mark, or `null` for no claim.
 *
 * `Object.hasOwn`, not a bare lookup: the value arrives from an API payload, and
 * `METRICS_SOURCE_NOTES["toString"]` would otherwise hand back an inherited
 * `Object.prototype` member and render a mark for a source that has none.
 */
export function metricsSourceNote(source) {
	if (typeof source !== "string") return null;
	return Object.hasOwn(METRICS_SOURCE_NOTES, source)
		? METRICS_SOURCE_NOTES[source]
		: null;
}
