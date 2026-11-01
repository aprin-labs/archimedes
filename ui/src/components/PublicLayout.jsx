import BrandMark from "./BrandMark";
import ThemeSwitcher from "./ThemeSwitcher.jsx";

export default function PublicLayout({ user, children }) {
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
						<BrandMark />
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
						<ThemeSwitcher />
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
			<PublicFooter />
		</div>
	);
}

export function PublicCallToAction() {
	return (
		<section className="public-final" aria-labelledby="final-title">
			<div className="public-shell public-final__layout">
				<div>
					<p className="public-final__eyebrow">Start with a brief.</p>
					<h2 id="final-title">Describe the portfolio you want to test.</h2>
				</div>
				<div>
					<a className="public-auth-link" href="/app/generate">
						Generate a strategy
					</a>
					<p>
						Arc public testnet only. Past performance is not a promise. A rigor
						gate can reject weak evidence; it cannot remove market risk.
					</p>
				</div>
			</div>
		</section>
	);
}

function PublicFooter() {
	return (
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
					{/* docs.archimedes-arc.com — our own S3 + CloudFront, not GitHub
					    Pages (#1634). The trailing slash is load-bearing: the docs
					    site uses mkdocs directory URLs, and the CloudFront function
					    in docs-site/infra/main.tf 301s the slashless form. Guarded by
					    ui/test/docs-link.test.js. */}
					<a
						href="https://docs.archimedes-arc.com/"
						target="_blank"
						rel="noreferrer"
					>
						Docs
					</a>
					<a href="/llms.txt">Agent API</a>
					<a href="/.well-known/agent.json">Agent manifest</a>
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
					<span>No privacy or terms page published</span>
				</nav>
			</div>
			<div className="public-shell public-footer__base">
				<span>
					Research prototype. No mainnet money. Generation fee is real testnet
					USDC.
				</span>
				<span>Past performance does not guarantee future results.</span>
			</div>
		</footer>
	);
}
