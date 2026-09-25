import {
	CORPUS_TOO_FEW_PAPERS,
	describeUnavailable,
	waysForward,
} from "../data/generationUnavailable";

// The error card for a run that stopped before synthesis.
//
// Owner's screenshot: the too-few-papers failure rendered as one red line and
// nothing else — no steer, no count, no way forward. This card is that outcome
// as a first-class result: what was asked, what lexical retrieval actually
// returned, and the two real moves (broaden the brief with corpus-derived
// terms; take a Surprise me brief).
//
// Scope note: this is the ERROR card only. The generation-stream event cards
// are owned elsewhere and are deliberately untouched here.

function SuggestionChips({ suggestions, steer, onBroaden }) {
	return (
		<div
			style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 8 }}
			aria-label="Corpus-derived suggestions"
		>
			{suggestions.map((s) => {
				// The count is measured, so it is shown. A chip that cannot act
				// (no handler wired) renders as a plain term rather than a
				// button that does nothing when pressed.
				const label = `${s.term} · ${s.papers} paper${s.papers === 1 ? "" : "s"}`;
				if (!onBroaden) {
					return (
						<span key={s.term} className="caption" style={{ opacity: 0.9 }}>
							{label}
						</span>
					);
				}
				return (
					<button
						key={s.term}
						type="button"
						className="btn btn-outline btn-sm"
						onClick={() => onBroaden(s.term, steer)}
						title={`Add "${s.term}" to your brief and edit it`}
					>
						{label}
					</button>
				);
			})}
		</div>
	);
}

export default function GenerationUnavailable({ data, onBroaden, onSurprise }) {
	const d = describeUnavailable(data);
	// Only the corpus-shortfall failure has more to say than the one-line
	// message the stream already renders above. A downed LLM backend or an
	// unavailable corpus gets that line and nothing else — there is no move
	// to offer, and a card full of non-advice is worse than no card.
	if (d.reasonCode !== CORPUS_TOO_FEW_PAPERS) return null;
	const ways = waysForward(data);

	return (
		<div
			className="info-box warning"
			style={{ marginTop: 12 }}
			data-testid="generation-unavailable"
		>
			<div className="label" style={{ marginBottom: 6 }}>
				No strategy was drafted, and nothing was saved
			</div>

			{d.candidatesFound !== null && (
				<p className="caption mb-0">
					Your brief: <em>{d.steer || "(no brief text)"}</em> — keyword
					{d.retrieval ? ` (${d.retrieval})` : ""} retrieval matched{" "}
					<strong>{d.candidatesFound}</strong> candidate paper
					{d.candidatesFound === 1 ? "" : "s"}
					{d.minPapers !== null ? ` of the ${d.minPapers} needed to fuse` : ""}.
				</p>
			)}

			{ways.length > 0 && (
				<div style={{ marginTop: 12 }}>
					{/* Counted, not asserted: with no corpus-derived terms to
					    offer, "two ways forward" would be one way forward and
					    a false headline. */}
					<div className="label" style={{ marginBottom: 6 }}>
						{ways.length === 1 ? "One way forward" : "Two ways forward"}
					</div>
					{ways.map((w) => (
						<div key={w.id} style={{ marginTop: 8 }}>
							<div style={{ fontWeight: 600 }}>{w.title}</div>
							<p className="caption mb-0">{w.detail}</p>
							{w.id === "broaden" && (
								<SuggestionChips
									suggestions={w.suggestions}
									steer={d.steer}
									onBroaden={onBroaden}
								/>
							)}
							{w.id === "surprise" && onSurprise && (
								<button
									type="button"
									className="btn btn-outline btn-sm"
									style={{ marginTop: 8 }}
									onClick={onSurprise}
								>
									Surprise me
								</button>
							)}
						</div>
					))}
				</div>
			)}
		</div>
	);
}
