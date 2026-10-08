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
			{/* Shell-level footer, moved here from Landing.jsx: it used to live on
			    one page, so Architecture (and not-found) carried no footer at all
			    and had nowhere to hang the policy links. Owning it at the shell
			    means every public page — Landing, Architecture, Privacy, Terms,
			    not-found — links to the policies, which is the point: Google's
			    OAuth consent review looks for a discoverable privacy link, not a
			    URL you only reach by typing it. */}
			<footer className="public-footer">
				<div className="public-shell public-footer__grid">
					<div className="public-footer__brand">
						<strong>Archimedes</strong>
						<p>Research-grounded strategy generation on Arc public testnet.</p>
					</div>
					<nav aria-label="Product links">
						<strong>Product</strong>
						<a href="/app/generate">Generate</a>
						<a href="/app/explore">Explore</a>
						<a href="/security">Security</a>
						<a href="/architecture">Architecture</a>
					</nav>
					<nav aria-label="Resource links">
						<strong>Resources</strong>
						<a href="/llms.txt">Agent API</a>
						<a href="/.well-known/agent.json">Agent manifest</a>
						{/* The docs site moved here with the rest of the footer when
						    the landing page stopped carrying its own (#1634 link,
						    guarded by ui/test/docs-link.test.js). */}
						<a
							href="https://docs.archimedes-arc.com/"
							target="_blank"
							rel="noreferrer"
						>
							Docs
						</a>
						<a
							href="https://github.com/aprin-labs/archimedes"
							target="_blank"
							rel="noreferrer"
						>
							GitHub
						</a>
					</nav>
					<nav aria-label="Project links">
						<strong>Project</strong>
						<a
							href="https://github.com/aprin-labs/archimedes/blob/main/LICENSE"
							target="_blank"
							rel="noreferrer"
						>
							Unlicense
						</a>
						<a href="https://faucet.circle.com/" target="_blank" rel="noreferrer">
							Arc faucet
						</a>
					</nav>
					<nav className="policy-links" aria-label="Policies">
						<strong>Policies</strong>
						<a href="/privacy">Privacy</a>
						<a href="/terms">Terms</a>
					</nav>
				</div>
				<div className="public-shell public-footer__base">
					<span>Research prototype. No mainnet money. Generation fee is real testnet USDC.</span>
					<span>Past performance does not guarantee future results.</span>
				</div>
			</footer>
		</div>
	);
}
