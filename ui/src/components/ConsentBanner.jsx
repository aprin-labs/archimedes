import { useId, useState } from "react";

import { useStorageConsent } from "../hooks/useStorageConsent.js";
import { STORAGE_INVENTORY } from "../storage-consent.js";
import ConsentChoices from "./ConsentChoices";

// First-visit storage-consent banner (#1647).
//
// Mounted once in main.jsx, OUTSIDE App, so it renders on every surface
// (public pages, auth screens, the app shell) rather than only where a
// particular layout happens to be used. It sits outside .public-site and
// .app-site, so it resolves the BASE palette — see the .consent-banner block
// at the end of App.css.
//
// It renders nothing once a choice is recorded. Changing your mind later is
// done from the storage disclosure on /security, which mounts the same
// ConsentChoices control; a banner that re-appears after you answered it is
// exactly the dark pattern this is supposed to be the opposite of.
//
// Deliberately NOT a portal and NOT a focus trap: it is a complementary
// region, not a modal. Blocking the page until a visitor answers is the
// other common dark pattern, and it would break the anonymous-OK front doors.
export default function ConsentBanner() {
	const [consent, decide] = useStorageConsent();
	const [customizing, setCustomizing] = useState(false);
	const panelId = useId();

	if (consent) return null;

	const acceptAll = () => decide({ functional: true, analytics: true });
	const rejectOptional = () => decide({ functional: false, analytics: false });

	const optionalCount = STORAGE_INVENTORY.filter(
		(entry) => entry.category === "functional" || entry.category === "analytics",
	).length;
	const necessaryCount = STORAGE_INVENTORY.filter(
		(entry) => entry.category === "necessary",
	).length;

	return (
		<aside
			className="consent-banner"
			aria-label="Browser storage consent"
			data-testid="consent-banner"
		>
			<div className="consent-banner__inner">
				<div className="consent-banner__copy">
					<h2 className="consent-banner__title">What this site stores</h2>
					<p>
						{necessaryCount} cookies and keys are strictly necessary — sign-in,
						wallet proof, and the checks that keep them correct. {optionalCount}{" "}
						more are optional: preferences, progress markers, and one anonymous
						drop-off counter. Until you choose, the optional ones are{" "}
						<strong>off</strong> and nothing optional is written.
					</p>
					<p className="consent-banner__note">
						No third-party trackers, no advertising, no consent SDK. Every key
						is listed with what it reveals in the{" "}
						<a href="/security#storage-disclosure">storage disclosure</a>.
					</p>
				</div>
				<div className="consent-banner__actions">
					<button
						type="button"
						className="consent-btn consent-btn--primary"
						onClick={acceptAll}
					>
						Accept all
					</button>
					<button type="button" className="consent-btn" onClick={rejectOptional}>
						Reject optional
					</button>
					<button
						type="button"
						className="consent-btn consent-btn--quiet"
						aria-expanded={customizing}
						aria-controls={panelId}
						onClick={() => setCustomizing((open) => !open)}
					>
						Customize
					</button>
				</div>
			</div>
			{customizing ? (
				<div className="consent-banner__panel" id={panelId}>
					<ConsentChoices onSaved={() => setCustomizing(false)} />
				</div>
			) : null}
		</aside>
	);
}
