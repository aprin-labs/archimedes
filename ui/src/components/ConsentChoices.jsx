import { useEffect, useId, useState } from "react";

import { useStorageConsent } from "../hooks/useStorageConsent.js";
import {
	CATEGORY_LABELS,
	CATEGORY_SUMMARIES,
	OPTIONAL_CATEGORIES,
	displayName,
	entriesInCategory,
} from "../storage-consent.js";

// The per-category switches (#1647). Rendered in two places — the banner's
// Customize panel and the /security disclosure section — from the SAME
// STORAGE_INVENTORY the gate itself reads, so the control can never offer a
// category the runtime does not actually enforce, and the key list under
// each switch can never drift from the code.
//
// Only OPTIONAL_CATEGORIES get a control. Strictly-necessary keys are shown
// in the disclosure table with their reason, never as a switch that would
// break sign-in if flipped (#1647 anti-goal 1).
export default function ConsentChoices({ onSaved }) {
	const [consent, decide] = useStorageConsent();
	const groupId = useId();
	const [draft, setDraft] = useState(() => ({
		functional: consent?.functional === true,
		analytics: consent?.analytics === true,
	}));
	const [savedAt, setSavedAt] = useState(null);

	// Another surface (the banner, or this page in a second tab) may record a
	// choice while this control is mounted; adopt it rather than leaving a
	// stale draft that would silently undo it on the next Save.
	useEffect(() => {
		setDraft({
			functional: consent?.functional === true,
			analytics: consent?.analytics === true,
		});
	}, [consent]);

	const save = () => {
		decide(draft);
		setSavedAt(Date.now());
		onSaved?.();
	};

	return (
		<div className="consent-choices">
			<fieldset className="consent-choices__set">
				<legend className="consent-choices__legend">
					Optional storage — your choice
				</legend>
				{OPTIONAL_CATEGORIES.map((category) => {
					const inputId = `${groupId}-${category}`;
					return (
						<div className="consent-choices__row" key={category}>
							<input
								className="consent-choices__input"
								type="checkbox"
								id={inputId}
								checked={draft[category]}
								onChange={(event) =>
									setDraft((prev) => ({
										...prev,
										[category]: event.target.checked,
									}))
								}
							/>
							<div className="consent-choices__body">
								<label className="consent-choices__label" htmlFor={inputId}>
									{CATEGORY_LABELS[category]}
								</label>
								<p className="consent-choices__summary">
									{CATEGORY_SUMMARIES[category]}
								</p>
								<ul className="consent-choices__keys">
									{entriesInCategory(category).map((entry) => (
										<li key={entry.name}>
											<code>{displayName(entry)}</code>
											<span>{entry.store}</span>
										</li>
									))}
								</ul>
							</div>
						</div>
					);
				})}
			</fieldset>
			<div className="consent-choices__actions">
				<button type="button" className="consent-btn" onClick={save}>
					Save choices
				</button>
				<p className="consent-choices__status" role="status">
					{savedAt
						? "Saved. Anything you switched off has been deleted from this browser."
						: consent
							? `Current choice: functional ${consent.functional ? "on" : "off"}, analytics ${consent.analytics ? "on" : "off"}.`
							: "No choice recorded yet — everything optional is currently off."}
				</p>
			</div>
		</div>
	);
}
