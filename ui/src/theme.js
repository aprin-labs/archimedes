import { canStore } from "./storage-consent.js";

export const THEME_STORAGE_KEY = "archimedes.theme";

export function normalizeTheme(choice) {
	return choice === "light" || choice === "dark" ? choice : "system";
}

// Follow the OS until the visitor chooses an explicit light/dark override.
export function getStoredTheme() {
	try {
		return canStore(THEME_STORAGE_KEY)
			? normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY))
			: "system";
	} catch {
		return "system";
	}
}

export function isThemeStored(choice) {
	try {
		return (
			canStore(THEME_STORAGE_KEY) &&
			localStorage.getItem(THEME_STORAGE_KEY) === choice
		);
	} catch {
		return false;
	}
}

export function resolveTheme(choice) {
	const normalized = normalizeTheme(choice);
	if (normalized !== "system") return normalized;
	try {
		return window.matchMedia("(prefers-color-scheme: dark)").matches
			? "dark"
			: "light";
	} catch {
		return "dark";
	}
}

// Rendering never writes storage: OS changes and shell remounts aren't choices.
export function renderTheme(choice) {
	const theme = resolveTheme(choice);
	const root = document.documentElement;
	root.setAttribute("data-theme", theme);
	root.dataset.appearance = normalizeTheme(choice);
	root.style.colorScheme = theme;
	document
		.querySelector('meta[name="theme-color"]')
		?.setAttribute(
			"content",
			getComputedStyle(root).getPropertyValue("--canvas").trim(),
		);
	return theme;
}

// False means current visit only (consent withheld OR storage unavailable).
export function applyTheme(choice) {
	const next = normalizeTheme(choice);
	renderTheme(next);
	try {
		if (canStore(THEME_STORAGE_KEY)) {
			localStorage.setItem(THEME_STORAGE_KEY, next);
			return isThemeStored(next);
		}
	} catch {
		// Blocked storage must not prevent the current-visit switch.
	}
	return false;
}

export function watchSystemTheme(onChange) {
	try {
		const media = window.matchMedia("(prefers-color-scheme: dark)");
		media.addEventListener("change", onChange);
		return () => media.removeEventListener("change", onChange);
	} catch {
		return () => {};
	}
}
