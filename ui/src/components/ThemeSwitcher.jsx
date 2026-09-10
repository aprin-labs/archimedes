import { useId } from "react";
import { useTheme } from "../ThemeContext.jsx";

export default function ThemeSwitcher() {
	const { choice, resolved, saved, choose } = useTheme();
	const descriptionId = useId();
	const nextTheme = resolved === "light" ? "dark" : "light";
	const label = `Switch to ${nextTheme} theme`;
	let storageMessage = saved
		? "Saved on this device."
		: "Not saved. This visit only. Functional storage must be allowed and available to remember it.";
	if (choice === "system")
		storageMessage =
			"Following your device appearance until you choose a theme.";

	return (
		<>
			<button
				type="button"
				className="appearance-trigger"
				onClick={() => choose(nextTheme)}
				aria-label={label}
				aria-describedby={descriptionId}
				title={`${label}. ${storageMessage}`}
			>
				<span
					className={resolved === "light" ? "i-lucide-moon" : "i-lucide-sun"}
					aria-hidden="true"
				/>
			</button>
			<span id={descriptionId} className="sr-only" role="status">
				Preference: {choice}. Current appearance: {resolved}. {storageMessage}
			</span>
		</>
	);
}
