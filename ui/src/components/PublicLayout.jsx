import { useState } from "react";

import { applyTheme, getStoredTheme } from "../theme";
import BrandMark from "./BrandMark";

export default function PublicLayout({ user, children }) {
	const [theme, setTheme] = useState(getStoredTheme);

	const toggleTheme = () => {
		const next = theme === "light" ? "dark" : "light";
		applyTheme(next);
		setTheme(next);
	};

	return (
		<div className="public-site">
			<a className="public-skip-link" href="#public-content">
				Skip to content
			</a>
			<div className="public-announcement" role="note">
				<span>Research prototype</span>
				<strong>Arc public testnet</strong>
				<span>No mainnet money</span>
			</div>
			<header className="public-header">
				<div className="public-header__inner">
					<a href="/" className="public-brand" aria-label="Archimedes home">
						<BrandMark className="public-brand__mark" />
						<span className="public-brand__copy">
							<strong>Archimedes</strong>
							<small>Research. Rigor. Proof.</small>
						</span>
					</a>
					<nav className="public-nav" aria-label="Public navigation">
						<a
							href="/#product"
							className="public-nav__link public-nav__section"
						>
							Product
						</a>
						<a
							href="/security"
							className="public-nav__link public-nav__section"
						>
							Security
						</a>
						<a href="/architecture" className="public-nav__link">
							Architecture
						</a>
						{/* The documentation site, served from our own infra
						    (docs-site/infra/main.tf, #1634). External host, so it
						    opens in a new tab and carries rel="noreferrer" like the
						    footer's off-site links. Guarded by
						    ui/test/docs-link.test.js. */}
						<a
							href="https://docs.archimedes-arc.com/"
							className="public-nav__link"
							target="_blank"
							rel="noreferrer"
						>
							Docs
						</a>
						<button
							type="button"
							className="public-theme-toggle"
							onClick={toggleTheme}
							aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}
							title={`Switch to ${theme === "light" ? "dark" : "light"} theme`}
						>
							<span
								className={theme === "light" ? "i-lucide-moon" : "i-lucide-sun"}
								aria-hidden="true"
							/>
						</button>
						<a className="public-sign-in" href={user ? "/app" : "/sign-in"}>
							{user ? "Open app" : "Sign in"}
						</a>
						<a className="public-auth-link" href="/app/generate">
							Generate a strategy
						</a>
					</nav>
				</div>
			</header>
			<div id="public-content" tabIndex="-1">
				{children}
			</div>
		</div>
	);
}
