import { useCallback, useEffect, useState } from "react";

import { readConsent, saveConsent, subscribeConsent } from "../storage-consent.js";

// React binding for the storage-consent gate (#1647).
//
// storage-consent.js is deliberately React-free — it is imported by plain
// modules (theme.js, config.js, circle-wallet.js) and by node --test, so it
// cannot pull in react. This hook is the only place the two meet.
//
// Returns `[consent, decide]`:
//   consent — null until the visitor answers the banner, then
//             { version, functional, analytics, decidedAt }.
//   decide  — saveConsent, which records the choice, purges anything the new
//             choice disallows, and notifies every mounted subscriber (so the
//             banner and the /security controls stay in sync without a reload).
export function useStorageConsent() {
	const [consent, setConsent] = useState(readConsent);

	useEffect(() => subscribeConsent(setConsent), []);

	const decide = useCallback((choice) => saveConsent(choice), []);

	return [consent, decide];
}
