import { useEffect, useState } from "react";
import { apiGet } from "../api";
import { ACCOUNT_USAGE_ENDPOINT, deriveFreeGenerationView } from "../freeGenerations";
import ResendVerificationControl from "./ResendVerificationControl";

// Free-generation allowance banner + remaining-count chip (#1643).
//
// Deliberately its own file with a SINGLE mount point in Generate.jsx: that
// page is being redesigned concurrently (#1642), so this feature's frontend
// footprint there is one import and one element.
//
// Everything shown here comes from GET /api/account/usage's
// free_generations_* fields — the same services/free_generations.py ledger
// that api/generate_routes.start_generation claims slots from, so the count a
// user reads and the count the gate enforces cannot drift apart. This
// component counts nothing itself.
//
// Three states, one element: available ("2 free generations left"), locked
// ("verify your email to unlock 3") and exhausted ("link a wallet"). The
// locked state is owner decision D1 (2026-08-31) — the free tier unlocks on a
// verified email, and this banner is where that unlock is offered rather than
// only being discovered as a 409.
//
// Renders NOTHING (not a placeholder, not a zero) when there is no honest
// number to show: signed out, request failed, the backend reported
// free_generations_remaining: null because the ledger was unreadable, or the
// free path is switched off (allowance <= 0). deriveFreeGenerationView in
// ../freeGenerations.js owns that decision and is unit-tested directly.
export default function FreeGenerationBanner({ email }) {
	const [view, setView] = useState(null);

	useEffect(() => {
		let cancelled = false;
		apiGet(ACCOUNT_USAGE_ENDPOINT)
			.then((usage) => {
				if (!cancelled) setView(deriveFreeGenerationView(usage));
			})
			.catch(() => {
				// 401 (signed out) and every transport failure land here. An
				// informational banner must never surface an error of its own,
				// and must never fall back to a guessed count.
				if (!cancelled) setView(null);
			});
		return () => {
			cancelled = true;
		};
	}, []);

	if (!view) return null;

	return (
		<div
			className="info-box mb-3 flex items-center justify-between gap-2"
			role="status"
			aria-live="polite"
			data-testid="free-generation-banner"
			// available | locked | exhausted — the state name the pure module
			// decided, exposed for styling and for a DOM test to assert on
			// without re-deriving the rule it is checking.
			data-state={view.state}
		>
			<div>
				<span>{view.message}</span>
				{view.state === "locked" && <ResendVerificationControl email={email} />}
			</div>
			<span
				className="caption"
				style={{ flexShrink: 0, whiteSpace: "nowrap", color: "var(--text-3)" }}
			>
				{view.chipLabel}
			</span>
		</div>
	);
}
