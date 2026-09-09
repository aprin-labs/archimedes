// Blocking, same-origin first paint; allowed by the existing script-src 'self'.
// Keep this small contract aligned with src/theme.js (theme.test.js runs both).
(function () {
	var choice = "system";
	try {
		var consent = JSON.parse(localStorage.getItem("archimedes.cookieConsent"));
		if (consent && consent.version === 1 && consent.functional === true) {
			var stored = localStorage.getItem("archimedes.theme");
			if (stored === "light" || stored === "dark" || stored === "system")
				choice = stored;
		}
	} catch (_) {
		// No usable override: follow the device without writing a preference.
	}
	var theme = choice;
	if (choice === "system") {
		try {
			theme = window.matchMedia("(prefers-color-scheme: dark)").matches
				? "dark"
				: "light";
		} catch (_) {
			theme = "dark";
		}
	}
	var root = document.documentElement;
	root.setAttribute("data-theme", theme);
	root.dataset.appearance = choice;
	root.style.colorScheme = theme;
	var metadata = document.querySelector('meta[name="theme-color"]');
	if (metadata)
		metadata.setAttribute(
			"content",
			getComputedStyle(root).getPropertyValue("--canvas").trim(),
		);
})();
