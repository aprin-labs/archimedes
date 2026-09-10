import {
	createContext,
	useContext,
	useEffect,
	useLayoutEffect,
	useState,
} from "react";
import { useStorageConsent } from "./hooks/useStorageConsent.js";
import { CONSENT_STORAGE_KEY } from "./storage-consent.js";
import {
	applyTheme,
	getStoredTheme,
	isThemeStored,
	normalizeTheme,
	renderTheme,
	resolveTheme,
	THEME_STORAGE_KEY,
	watchSystemTheme,
} from "./theme.js";

const ThemeContext = createContext(null);

// Lives above routing: switching shells, signing in, or revoking consent must
// not reset a visit-only choice (or remount the user's working screen).
export function ThemeProvider({ children }) {
	const [choice, setChoice] = useState(() =>
		normalizeTheme(
			document.documentElement.dataset.appearance ?? getStoredTheme(),
		),
	);
	const [resolved, setResolved] = useState(() => resolveTheme(choice));
	const [chosen, setChosen] = useState(() => isThemeStored(choice));
	const [saved, setSaved] = useState(() => isThemeStored(choice));
	const [consent] = useStorageConsent();
	const [storageRevision, setStorageRevision] = useState(0);

	useLayoutEffect(() => {
		setResolved(renderTheme(choice));
		setSaved(chosen ? applyTheme(choice) : isThemeStored(choice));
	}, [choice, chosen, consent, storageRevision]);

	useEffect(() => {
		if (choice !== "system") return;
		const update = () => setResolved(renderTheme(choice));
		const stop = watchSystemTheme(update);
		update();
		return stop;
	}, [choice]);

	useEffect(() => {
		const update = (event) => {
			if (event.key === CONSENT_STORAGE_KEY || event.key === null) {
				setStorageRevision((value) => value + 1);
			} else if (event.key === THEME_STORAGE_KEY) {
				// Removed, invalid or unconsented data must not reset this visit
				// or leave the control claiming that its choice is still saved.
				const next = getStoredTheme();
				const remembered = isThemeStored(next);
				if (remembered) setChoice(next);
				setSaved(remembered);
			}
		};
		window.addEventListener("storage", update);
		return () => window.removeEventListener("storage", update);
	}, []);

	const choose = (next) => {
		setChosen(true);
		setChoice(normalizeTheme(next));
	};

	return (
		<ThemeContext.Provider value={{ choice, resolved, saved, choose }}>
			{children}
		</ThemeContext.Provider>
	);
}

export function useTheme() {
	const value = useContext(ThemeContext);
	if (!value) throw new Error("useTheme must be used inside ThemeProvider");
	return value;
}
