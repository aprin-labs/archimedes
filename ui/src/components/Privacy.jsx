import PolicyBanner from "./PolicyBanner";

// /privacy — the public privacy policy.
//
// EVERY factual claim on this page was written from the code, and the PR that
// introduced it carries a claim-by-claim evidence map (statement → file:line).
// That is the standard to hold it to when editing: if you change what the
// software does, this page is part of the change, and if you change this page,
// name the code that makes the new sentence true.
//
// Two things that are easy to get wrong here, both of which were wrong in the
// first draft of this page's outline:
//   - Cookies are NOT session-only. There is a second cookie, archimedes_vid
//     (backend/archimedes/api/funnel_middleware.py), with a 180-day lifetime.
//   - IP addresses ARE stored. Not in the visitor-counting pipeline — that one
//     genuinely never sees an IP — but on every sign-in session row
//     (auth_sessions.ipAddress) and in the rate-limit / quota keys.
// Saying otherwise would be the comfortable version, not the true one.
export default function Privacy() {
	return (
		<div className="page-content policy-page">
			<PolicyBanner />

			<header>
				<p className="public-kicker">Privacy</p>
				<h1>Privacy Policy</h1>
				<p className="policy-meta">
					Last updated: [pending owner approval] · Archimedes
					(archimedes-arc.com) is operated under the name APRIN&nbsp;Labs, which
					is not a registered company
				</p>
			</header>

			<section>
				<h2>The short version</h2>
				<p>
					Archimedes is a research tool. We collect what we need to run an
					account, generate strategies, and stop abuse — and not much else.
					There are no advertising trackers or third-party analytics scripts on
					this site. We do not sell your data.
				</p>
				<p>
					We are also going to be specific about the parts that are less
					comfortable: we store your IP address on each sign-in, we set a
					long-lived cookie to count visitors, and there is currently no
					self-service delete button. All three are described below.
				</p>
			</section>

			<section>
				<h2>What we collect when you create an account</h2>
				<ul>
					<li>
						<strong>Your name and email address.</strong> Email is used to sign
						you in, verify the address, and send password resets. We do not send
						marketing email.
					</li>
					<li>
						<strong>Your password, hashed.</strong> We never store the password
						itself, and we cannot read it. Minimum length is 12 characters.
					</li>
					<li>
						<strong>Whether your email is verified,</strong> and a profile image
						URL if your sign-in provider supplied one.
					</li>
					<li>
						<strong>A record of each sign-in session</strong> — a session token,
						when it expires, and the IP address and browser user-agent the
						session was created from. Sessions last seven days.
					</li>
				</ul>
				<p>
					You can optionally add a display name, a list of research interests,
					how you heard about us, and a separate contact email. That contact
					email is encrypted before it is written to the database.
				</p>
			</section>

			<section>
				<h2>If you sign in with Google or GitHub</h2>
				<p>
					We ask the provider only for the default sign-in scopes — enough to
					learn your email address, your name, and your avatar. We do not
					request access to your files, repositories, contacts, or anything
					else, and we have not configured any extended scopes.
				</p>
				<p>
					We store the provider&rsquo;s account identifier for you, the scope it
					granted, and the tokens it issued. Those tokens are encrypted before
					they are stored.
				</p>
				<p>
					<strong>Linking is always explicit.</strong> Signing in with Google
					using an email address that already has a password account here does
					not silently merge the two — that sign-in is refused rather than
					joined. Linking a second sign-in method to an existing account is an
					action you take while already signed in, and a linked provider must
					use the same email address as the account. We do this so that
					controlling an email address at one provider can never quietly become
					control of your Archimedes account.
				</p>
			</section>

			<section>
				<h2>If you link a wallet</h2>
				<p>
					Linking a wallet means signing a message to prove you control it. We
					store the wallet address, its chain, which wallet software you used,
					and the time it was verified. The one-time challenge you sign is
					stored only as a hash — the challenge text itself is never kept.
				</p>
				<p>
					We also keep an append-only ledger of wallet activity: the first time
					an address was seen, the last time it proved control, and the events
					it took part in (a generation started, a vault created, a strategy
					published). At the moment a wallet proves control, the anonymous
					visitor id described below is linked to it in that ledger. Before that
					moment, it is not.
				</p>
				<p>
					A wallet already linked to another account cannot be taken over by
					linking it again; that request is refused rather than transferred.
				</p>
			</section>

			<section>
				<h2>When you pay for a generation</h2>
				<p>
					Generating a strategy is charged to your linked wallet — currently
					$2.00 in testnet USDC — and that payment really settles. The terms
					describe what that means for your funds; this section is about the
					record it leaves.
				</p>
				<p>
					Every settled payment is written to a{" "}
					<code>payment_receipts</code> row and kept, so that you can look back
					at what you were charged. Each row holds:
				</p>
				<ul>
					<li>your account id, and the wallet address that paid;</li>
					<li>
						the amount and the price it was charged at, and the network it was
						charged on;
					</li>
					<li>
						the settlement reference our payment provider returned — an
						identifier for the transfer, not an on-chain transaction hash;
					</li>
					<li>
						which generation the payment funded, and when the payment settled.
					</li>
				</ul>
				<p>
					A second record, <code>generation_credits</code>, is the ledger that
					decides what you are owed. A settled payment buys a credit and a
					generation spends it, so that a run which takes your money and then
					fails leaves the credit with you rather than leaving you out of pocket.
					Alongside the payment details above, a credit row holds its state
					(claimed, available, spent, or voided), the times it changed hands,
					which generation spent it, and — if your client sent one — the
					idempotency key it used, which is how a retried request is recognised
					as the same charge instead of becoming a second one.
				</p>
				<p>
					You can read your own receipts back in the app, and no other account
					can see them. These rows have no expiry date — they persist until your
					account is deleted, and removing them is part of the deletion process
					described below. One honest limitation: writing the receipt is never
					allowed to fail or delay the generation you paid for, so if our
					database is unavailable at that moment a payment can settle without a
					receipt row. A missing receipt is a gap in the record, not evidence
					that no charge happened.
				</p>
			</section>

			<section>
				<h2>What you make on Archimedes</h2>
				<p>
					We store what you put in and what comes out: the brief you write, the
					strategy the system generates, its backtest results and rigor verdicts,
					the reasoning trace behind it, and any chat you have with the agent.
					This is the product — it is stored so you can come back to it.
				</p>
				<p>
					<strong>Your brief is sent to a language model to be answered.</strong>{" "}
					Today that model runs on Amazon Bedrock, inside the same AWS account
					that runs the rest of the service. Your brief text is part of the
					prompt sent to it.
				</p>
				<p>
					We keep every generation attempt, including the ones that fail the
					rigor gate and the ones you reject — the brief text, the resulting
					strategy specification, the papers it drew on, and the verdict. Keeping
					the failures is deliberate: a system that only remembers its successes
					cannot tell you how selective it was being.
				</p>
				<p>
					We also record a measurement of what each generation run consumed:
					token counts and elapsed seconds. That record is deliberately
					measurement only. A check runs over it before it is saved and raises an
					error if anything price-shaped is present, so the measurement cannot
					quietly become a bill. Your prompt and the model&rsquo;s response text
					are not part of it.
				</p>
			</section>

			<section>
				<h2>Counting visitors</h2>
				<p>
					We want to know how many people reach the site and how far they get.
					We do this without keeping a record of individuals:
				</p>
				<ul>
					<li>
						Your browser is given a random, opaque id in a cookie named{" "}
						<code>archimedes_vid</code>. It is not derived from your IP address,
						your device, or anything about you. It lasts 180 days and cannot be
						read by JavaScript.
					</li>
					<li>
						That id is fed into probabilistic distinct-count sketches
						(HyperLogLog) for each funnel stage. The sketches count how many
						distinct visitors reached a stage; they cannot be read back to
						produce the list of ids that went in.
					</li>
					<li>
						Country comes from a two-letter code that our CDN derives from the
						connection and passes on as a header. We do not run an IP-geolocation
						lookup, and the servers behind the CDN cannot see your real IP at
						all on that path.
					</li>
					<li>
						Device class (mobile, tablet, desktop) comes from the same CDN
						headers, falling back to a coarse read of the user-agent string. The
						user-agent string itself is not stored by this pipeline — only which
						of those buckets it fell into.
					</li>
				</ul>
				<p>
					This pipeline never reads your IP address. Daily counts expire after
					90 days; running totals do not expire.
				</p>
			</section>

			<section>
				<h2>IP addresses</h2>
				<p>
					Being straightforward about this, because the section above is easy to
					misread as &ldquo;we never touch IPs&rdquo;:
				</p>
				<ul>
					<li>
						Your IP address is stored on each sign-in session record, alongside
						your browser user-agent.
					</li>
					<li>
						Your IP address is used as a rate-limiting key, and as the key for a
						daily cap on how many generations can be started from one address.
						Those keys live in a cache and expire on their own — the
						rate-limit counters within minutes, the daily generation cap within
						36 hours.
					</li>
					<li>
						When that daily cap is hit, the IP address is written to our
						application log.
					</li>
					<li>
						Our web server and load balancer keep standard access logs, which
						include IP addresses. Load balancer logs are deleted after 30 days;
						application and web server logs after 90 days.
					</li>
				</ul>
				<p>
					All of this exists to keep accounts secure and to stop one person from
					draining a shared resource. None of it is used to profile you or shared
					with advertisers.
				</p>
			</section>

			<section>
				<h2>Cookies and browser storage</h2>
				<p>Two cookies, both set by our own servers and both hidden from JavaScript:</p>
				<ul>
					<li>
						<strong>The sign-in session cookie.</strong> Present once you sign
						in, expires after seven days.
					</li>
					<li>
						<strong>
							<code>archimedes_vid</code>
						</strong>
						, the anonymous visitor id described above, 180 days.
					</li>
				</ul>
				<p>
					We also keep some things in your browser&rsquo;s own storage that never
					leave your device unless you send them to us: your light/dark theme
					choice, which wallet you last connected and any nickname you gave it,
					whether you have finished the onboarding tour, your rigor-strictness
					preference, and, if you use a Circle passkey wallet, that
					wallet&rsquo;s credential. None of these are tracking identifiers.
				</p>
			</section>

			<section>
				<h2>Anything published is public and permanent</h2>
				<p>
					When you deploy a vault, publish a reasoning trace, or transact, that
					goes onto the Arc public testnet. This is the point of the product —
					provenance you can verify without trusting us — but it has consequences
					worth stating plainly.
				</p>
				<p>
					<strong>On-chain records are public and permanent.</strong> Anyone can
					read them. We cannot edit or delete them, and neither can you. A
					deletion request reaches our database; it cannot reach a blockchain.
				</p>
				<p>What actually gets published, so there are no surprises:</p>
				<ul>
					<li>
						<strong>Vault and contract addresses,</strong> and the fact that a
						particular wallet created a vault. That is an ordinary public event
						on any chain.
					</li>
					<li>
						<strong>Hashes,</strong> not documents, in contract storage — the
						registry holds a fingerprint of a strategy&rsquo;s methodology and of
						the paper corpus behind it, never the strategy itself.
					</li>
					<li>
						<strong>Revealed trace content, inside the transaction itself.</strong>{" "}
						The commit-then-reveal flow proves a decision was made before its
						outcome was known, and the reveal transaction carries the trace
						contents so anyone can check the hash. That includes the
						model&rsquo;s written reasoning, the market context, the capital
						figure, and the portfolio weights before and after. It is not kept in
						contract storage, but transaction history is public and permanent, so
						treat it as published. Your original free-text brief is deliberately
						excluded from what gets hashed and revealed.
					</li>
					<li>
						<strong>A public provenance record on IPFS.</strong> A reduced version
						of a trace — the papers cited, the methodology, the rigor scores, the
						vault address, and the model&rsquo;s reasoning — is pinned to IPFS and
						its address anchored on-chain. Position sizing, weights and code
						hashes are deliberately left out. Pinned content is public and, in
						practice, permanent.
					</li>
				</ul>
				<p>
					<strong>A wallet address is pseudonymous, not anonymous.</strong> It is
					not your name, but everything that address has ever done is linkable —
					on Arc and on every other chain it has been used on. If you have
					attached that address to your identity anywhere else, that link follows
					it here.
				</p>
			</section>

			<section>
				<h2>Who else is in the path</h2>
				<p>These are the third parties your data actually reaches:</p>
				<ul>
					<li>
						<strong>Amazon Web Services (US East region).</strong> The whole
						service runs there — application servers, the database, the cache,
						logs, and the CDN. In practice AWS holds everything described on this
						page.
					</li>
					<li>
						<strong>Amazon SES,</strong> which delivers your verification and
						password-reset emails. It receives your email address and the
						contents of those messages.
					</li>
					<li>
						<strong>Amazon Bedrock,</strong> which runs the language model. Your
						brief and the context around it are sent to it as a prompt. Bedrock
						is part of the same AWS account, so this does not hand your text to a
						separate vendor by default.
					</li>
					<li>
						<strong>Google or GitHub,</strong> only if you choose to sign in with
						them, and only for what that sign-in requires.
					</li>
					<li>
						<strong>Circle,</strong> if you use a Circle-backed wallet. Creating a
						passkey wallet registers a public key and a generated username with
						Circle from your browser; it does not send them your email address or
						password. Separately, our own operator wallet submits on-chain
						transactions through Circle, which sees vault addresses and
						transaction data.
					</li>
					<li>
						<strong>IPFS pinning,</strong> for the public provenance records
						described above.
					</li>
					<li>
						<strong>The Arc testnet network.</strong> Your browser talks
						directly to the Arc testnet RPC endpoint and, when you follow a
						transaction link, to the Arc block explorer. Those services see your
						IP address and what you asked the chain about.
					</li>
					<li>
						<strong>Google Fonts — linked, but blocked.</strong> The page markup
						still asks for typefaces from Google&rsquo;s font CDN, and we would
						rather delete that line than explain it. As the site is served today
						our content security policy does not permit it: styles and font
						files may load only from our own origin, so both the stylesheet and
						the fonts are refused before they are fetched and the page renders
						in the fallback typefaces already on your device. What can still
						reach Google is the connection hint the markup opens ahead of that
						blocked load — a network connection Google may see your IP address
						from, carrying nothing about the page. Removing the link is tracked
						separately; until it is gone, this is the accurate description.
					</li>
				</ul>
				<p>
					Market price data is pulled by our servers from public market-data
					sources. That is a one-way request we make; nothing about you goes with
					it.
				</p>
			</section>

			<section>
				<h2>What we do not do</h2>
				<ul>
					<li>
						<strong>No advertising or analytics trackers.</strong> There is no
						Google Analytics, no Segment, no Mixpanel, no PostHog, no Facebook
						pixel, and no error-reporting SaaS on this site. The browser is also
						blocked from running third-party scripts by our content security
						policy, so a tracker could not be added by accident.
					</li>
					<li>
						<strong>We do not sell your data,</strong> and we do not share it for
						advertising.
					</li>
					<li>
						<strong>We do not buy or import contact lists.</strong> The only
						addresses we ever email are ones people typed in themselves.
					</li>
				</ul>
			</section>

			<section>
				<h2>How long we keep things, and how to get them deleted</h2>
				<p>Some things expire on their own:</p>
				<ul>
					<li>Sign-in sessions, after seven days.</li>
					<li>Rate-limit counters, within minutes; daily generation caps, within 36 hours.</li>
					<li>Daily visitor counts, after 90 days.</li>
					<li>Load balancer access logs, after 30 days; application and web server logs, after 90 days.</li>
				</ul>
				<p>
					<strong>Everything else persists until you ask us to delete it.</strong>{" "}
					Your account, your strategies, every generation attempt including the
					rejected ones, your profile, your payment receipts, and stored
					reasoning traces have no expiry date today.
				</p>
				<p>
					We are describing what we have rather than a policy we have not built:{" "}
					<strong>
						there is no delete-my-account button and no automated deletion job.
					</strong>{" "}
					Deletion today is manual, done by a person, on request. Ask through the
					contact route below and we will remove your account and everything
					attached to it.
				</p>
				<p>
					It is worth being precise about what &ldquo;everything attached&rdquo;
					means, because two kinds of record are handled differently. Records
					that exist only for you are erased: your sessions, your linked sign-in
					providers, your linked wallets, your profile row — which is the one
					holding your encrypted contact email — your paper-trading history, and
					your payment receipts and credits. Records that other accounts can
					reference by id
					are detached from you rather than destroyed: your strategies, their
					passports, the generation records behind them, and vault descriptions
					keep existing with the link to you removed, so that deleting your
					account cannot break someone else&rsquo;s. If you want those erased as
					well rather than detached, say so and they will be. Some of this the
					database now does by itself when the account row goes and some of it is
					still done by hand as part of the request — which is which is an
					implementation detail we are actively closing, and it does not change
					what you end up with.
				</p>
				<p>
					Two limits, stated up front: published records cannot be deleted by
					anyone once they are on a blockchain or pinned to IPFS (see above), and
					log entries age out on the schedules listed rather than being pulled out
					individually.
				</p>
				<p>
					A self-service deletion and export path is work we intend to do. Until
					it exists, this page will keep describing the manual process, because
					describing the one we mean to build would be a claim about software that
					is not running.
				</p>
			</section>

			<section>
				<h2>Security</h2>
				<p>
					Passwords are hashed. Sign-in provider tokens and your optional contact
					email are encrypted before storage. Both cookies are marked HttpOnly
					and, in production, Secure. Sign-in and sign-up are rate limited, and
					personal fields are stripped from our logs before they are written.
				</p>
				<p>
					Archimedes runs on a public testnet and is early software. Please do
					not connect a wallet holding assets you care about, and please do not
					put anything in a prompt that you would not want stored.
				</p>
			</section>

			<section>
				<h2>Children</h2>
				<p>
					Archimedes is not intended for anyone under 18, and we do not knowingly
					collect information from children.
				</p>
			</section>

			<section>
				<h2>Changes to this policy</h2>
				<p>
					When what the software does changes, this page changes with it. The
					&ldquo;last updated&rdquo; line at the top is the record of that. If a
					change materially affects what we collect or who receives it, we will
					say so rather than editing quietly.
				</p>
			</section>

			<section>
				<h2>Contact</h2>
				<p>
					Privacy questions, and deletion or access requests, go to{" "}
					<a href="mailto:privacy@archimedes-arc.com">
						privacy@archimedes-arc.com
					</a>
					. That is a private mailbox and the right route for anything involving
					your account or your personal details.
				</p>
				<p>
					If you would rather raise something in the open — a question about this
					policy itself, or a mistake you have spotted on this page — the
					project&rsquo;s issue tracker works too:{" "}
					<a
						href="https://github.com/aprin-labs/archimedes/issues"
						target="_blank"
						rel="noopener noreferrer"
					>
						github.com/aprin-labs/archimedes/issues
					</a>
					. It is public, so please do not post personal details there; use the
					email address above for those.
				</p>
			</section>
		</div>
	);
}
