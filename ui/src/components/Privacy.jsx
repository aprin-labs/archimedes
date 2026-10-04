import PolicyBanner from "./PolicyBanner";

// /privacy — the public privacy policy.
//
// Every factual sentence here is a claim about main PLUS the live stack, and
// the 2026-10-01 rewrite (#1432) re-checked each one against both: the code
// that does the thing, and the live AWS configuration where code is not the
// whole story (log retention, backups, the firewall, SES, the privacy@ relay).
// If you change what the software does, this page is part of the change; if
// you change this page, name the code that makes the new sentence true.
// ui/test/policy-pages.test.js pins the load-bearing sentences.
//
// The sentences most likely to rot, and what makes them true today:
//   - "We store your IP address on each sign-in session". Since #1908 nginx
//     trusts CloudFront's origin-facing ranges (nginx/nginx.conf, "Real client
//     IP behind CloudFront"), so X-Client-IP, which Better Auth stores as
//     auth_sessions.ipAddress, is the viewer. Better Auth keeps an IPv6
//     address as its /64 (auth/auth.js advanced.ipAddress.ipv6Subnet). Before
//     #1908 this sentence was false: the stored value was the CloudFront edge.
//   - Expired sessions are deleted by auth/session-sweep.js (hourly). The
//     Google ID token is dropped before every write (auth/auth.js
//     dropIdToken) and old ones were cleared by alembic 7d2f9a4c1e60.
//   - The deletion lists mirror ui/src/account-deletion.js, which
//     ui/test/account-deletion.test.js pins against the models' ON DELETE
//     actions; its DELETION_RETAINED (what Account Settings shows before a
//     deletion) names the same four not-touched database records as this
//     page. payment_receipts, generation_credits, free_generation_grants
//     and the wallet ledger (wallet_identities / identity_events) carry no FK
//     to auth_users, so account deletion does not reach them. Credits are
//     looked up only by user_id (take_available_credit), so after deletion
//     an unspent one cannot be used, as the not-touched bullet says and the
//     dialog's generation_credits label does too. Detached rows
//     keep owner_wallet (vault_metadata: creator_address), the brief text and
//     any strategy or vault name the user typed, and they re-attach to any
//     account that later links that wallet: _link_verified_wallet calls
//     claim_legacy_wallet_data (api/wallet_routes.py), which stamps
//     owner_user_id on every row whose wallet column is that address and
//     whose owner_user_id is NULL. Linking needs a wallet signature.
//   - The hand-back of a failed free or paid run carries the same qualifier
//     as Terms.jsx (see its header): a server, database or cache failure can
//     keep either from coming back.
//   - Account Settings lists sessions only for a sign-in younger than 24
//     hours: better-auth's /list-sessions uses freshSessionMiddleware, and
//     auth/auth.js pins freshAge at 24h (session.freshAge); the stale state's
//     copy is SESSIONS_STALE_MESSAGE in AccountSettings.jsx.
//   - Wallet-link challenge fields: models/account.py WalletLinkChallenge
//     (nonce_hash, address, chain_id, provider, circle_wallet_id, domain,
//     uri, issued_at, expires_at, consumed_at).
//   - Account deletion does not reach the paper-trading reasoning records in
//     Redis either, and Account Settings does not list them (they are not a
//     table). advance_deployment -> _publish_decision_traces
//     (services/paper_trading.py:569) -> publish_paper_trace -> _save
//     (services/paper_trace.py:529) -> AgentStateStore.save_trace
//     (services/redis_state.py:536): a plain SET of archimedes:trace:<hash>
//     with no TTL, holding owner_user_id and owner_wallet, plus a ZADD to
//     archimedes:trace:index. The cascade removes only the Postgres index
//     (paper_decision_traces: trace_id/trace_hash). Follow-up: purge on delete.
//   - "On request, we will erase or scrub what account deletion leaves, send
//     you a copy, or both" is done BY HAND by the operator. Proof first: for
//     the account, the requester answers a message sent to its email address
//     (and the copy goes only there); for a wallet, they sign a message with
//     it, which, while the account exists, covers only that wallet's records.
//     A passkey wallet is a smart account: verify its signature with the
//     ERC-1271/6492 check in api/_erc6492.py, not plain ecrecover. The site
//     gives a passkey holder no way to sign a message we send (signSiweMessage,
//     ui/src/config.js:562, is called only by the link flow), so a passkey
//     holder proves control by linking the passkey wallet to an account (a
//     new one if theirs is deleted); the linked_wallets row is the proof.
//     Find its device payment key, the payer_wallet on its receipts, from the
//     smart account's depositFor calls on-chain (ui/src/x402.js:195-202).
//     The message to sign must move no funds and approve nothing (the page
//     promises it, and that we never ask for a transaction): a plain text
//     message, verified as above. An erasure request proven through the
//     account (its email address): delete the account first if it still exists
//     (deleting a live account's free_generation_grants would hand it its free
//     runs again), then follow the steps below. A request proven only by a
//     wallet signature while the account still exists: do NOT delete the
//     account unless its holder asks through the account's email address, and
//     erase or scrub only what names that wallet (its identity_events and
//     strategy_generators rows, payment receipts and credits whose
//     payer_wallet is that address, the Redis records whose owner_wallet is
//     it, and its address on the detached rows below); leave the account's own
//     rows, its free_generation_grants included, and the wallet_identities row
//     while a linked_wallets row still points at it. After deletion, a wallet
//     signature authorises erasure of the rows under the account id recovered
//     through that wallet (payment_receipts / generation_credits.payer_wallet,
//     or identity_events.meta->>'job_id' and strategy_proposals.generation_id
//     joined to free_generation_grants.job_id); a copy covers only rows that
//     name that wallet. What to change, checked against the models and
//     migrations (no constraint blocks any of it; the only trigger in the
//     schema is the BEFORE DELETE on auth_users, migration 85ca5310b7a1):
//       Delete the rows (nothing has an FK into these tables):
//         payment_receipts, generation_credits, free_generation_grants
//           WHERE user_id = the account id (recovered as above after
//           deletion);
//         identity_events WHERE wallet = the address;
//         strategy_generators WHERE wallet_address = the address
//           (wallet_address is NOT NULL, so the row goes rather than the
//           value).
//       Redis: for every archimedes:trace:<hash> whose JSON owner_user_id is
//         the account id or owner_wallet is the address: DEL
//         archimedes:trace:<hash> and archimedes:trace:id:<id>, ZREM
//         archimedes:trace:index <hash>. (After deletion the
//         paper_decision_traces rows that index them are gone, so scan.)
//       Find the detached rows. Those made with a wallet linked carry it in
//       owner_wallet. Those made before any wallet was linked have neither
//       owner_wallet nor owner_user_id; find them by job id: job_id from the
//       three FK-less tables -> strategy_proposals.generation_id, and
//       generation_costs(job_id, strategy_id) -> strategy_store /
//       strategy_passports.id.
//       Clear brief text, typed strategy and vault names and the wallet on
//       detached rows:
//         strategy_store.brief_intent (nullable) -> NULL;
//         strategy_store.strategy_name (NOT NULL, default '') -> '' (the
//           winner carries the name typed into Generate's "Strategy name"
//           field, agents/generation_pipeline.py _resolve_name);
//         strategy_store.owner_wallet (nullable, FK) -> NULL;
//         strategy_proposals.payload is NOT NULL JSON text holding the brief
//           under "intent" and the name under "strategy_spec"."strategy_name"
//           and the top-level "strategy_name" (persist_proposal spreads its
//           extra dict into the stored payload,
//           services/strategy_memory.py:93-100;
//           agents/generation_pipeline.py:1732, :1749): rewrite the JSON with
//           those set to "" (no code looks "intent" up; the proposals API
//           returns the JSON as stored);
//         strategy_proposals.owner_wallet (nullable, no FK) -> NULL;
//         strategy_passports.owner_wallet and curator_wallet (nullable, FK)
//           -> NULL (no brief or name column on this table);
//         vault_metadata.name (String(64)) and .symbol (String(16)), both
//           NOT NULL default '' -> '' (typed in CreateVaultModal and stored
//           by POST /api/vaults/metadata, models/chat.py VaultMetadata);
//         vault_metadata.creator_address is NOT NULL and FKs to
//           wallet_identities, so it cannot be NULL or "": overwrite it with
//           'system', the sentinel identity migration 07e9c1489199 created
//           for exactly this (no brief column on this table).
//       Then delete the wallet_identities row. Every FK into it must be
//       cleared first: the columns above, plus linked_wallets.address (goes
//       with the account) and user_profiles.wallet_address (DELETE any
//       user_profiles row WHERE wallet_address = the address: an unclaimed
//       one, owner_user_id NULL, survives account deletion when the account
//       never linked that wallet, and it holds an encrypted contact email),
//       paper_deployments.owner_wallet, marketplace_agents.creator_wallet /
//       subscriber_wallet and controlled_wallets.controller_wallet (all
//       nullable), and chat_messages.wallet_address (the table is empty).
//     Not named on this page, but holding the address: legacy_row_adoptions.
//     prior_owner_wallet (NOT NULL, no FK; only for rows of wallets that were
//     never linked); delete the row if the request covers it.
//   - Three free generations, the $2.00 price and the 100/200 daily caps are
//     pinned in infra/ecs.tf (FREE_GENERATIONS_PER_ACCOUNT,
//     GENERATION_PRICE_USD, GENERATION_DAILY_CAP_PER_USER/_PER_IP).
//   - The visitor-id marker expires one cookie lifetime after it is written
//     (services/visitor_insights_store.py, #1908). The /security inventory
//     (ui/src/storage-consent.js) is the full key list this page points to.
//   - The visitor id is in no ledger row, so "Entries in the ledger do not
//     include the anonymous visitor id" carries no "New" and "We do not link
//     the visitor id" carries no exception. The only code that writes
//     identity_events.vid is api/auth_siwe.py's verify (vid= at :402), and
//     that router is mounted only under TESTING (main.py:880). Alembic
//     5cdd4f098a74 (#1926) ran in prod on 2026-10-03 and nulled the vid on
//     the 20 auth_verified rows from the SIWE window; the read-only
//     scripts/count_privacy_leftovers.py then printed 0 for both of its
//     identity_events counts. Those two WHERE clauses (in-window
//     auth_verified rows with a vid, and every other row with a vid) cover
//     every row with a non-null vid between them, because event_type and
//     occurred_at are NOT NULL (models/identity.py:136, :138).
//   - The server-counted funnel steps the analytics bullet lists are the ones
//     that can fire on the live site: wallet_gate_shown, generation_started
//     and free_generation_used (api/generate_routes.py:534, :674, :680).
//     wallet_connected is recorded only by the test-only SIWE router, and
//     vault_deployed (api/vaults_routes.py:418) sits behind the roadmap gate
//     described below.
//   - privacy@ is an SES receipt rule -> SNS -> one email subscription, a
//     personal Gmail inbox (infra/ses_inbound.tf). SNS actions bounce mail
//     over 150 KB.
//   - NOT on the page, on purpose: that our database records a bounce or
//     spam report on the account and refuses sign-up or the signed-in resend
//     on it. Audit round 1 added that sentence; the final triaged pass took it
//     out. The code exists (auth/auth.js bounceRefusal :270, signup :754,
//     resend :786; the scripts/ses_events.py drain that writes
//     auth_users.emailBouncedAt / emailBounceKind; infra/ses_events.tf), but
//     it is not applied live. Checked 2026-10-03: the archimedes-mail
//     configuration set and the SQS queue exist, but there is no event
//     destination, no ses-events-drain task definition and no scheduler
//     schedule, so the stamp is never written in production. Re-add the
//     sentence in the same change that applies #1837.
//   - "Bedrock's own prompt logging is switched off in our account": on
//     2026-10-03, aws bedrock get-model-invocation-logging-configuration
//     returned an empty configuration (rc 0), meaning no invocation logging,
//     in us-east-1, us-east-2 and us-west-2, the regions a brief can reach.
//   - The load balancer's log fields: infra/alb.tf enables access_logs (S3,
//     expired after 30 days), and AWS's fixed ALB log format includes the
//     request line and user_agent. Every CloudFront behaviour uses the
//     all_viewer origin request policy (allViewerAndWhitelistCloudFront,
//     infra/cloudfront.tf), so the user-agent is the viewer's.
//   - Added by the 2026-10-03 audit round 1: the referrer is
//     "$log_http_referer" in nginx's main_redacted format
//     (nginx/nginx.conf:125). The docs site's unpkg request: docs pins
//     mkdocs-material 9.7.7 (docs/requirements.txt:15), whose bundle loads
//     unpkg.com/mermaid@11 for the mermaid fence mkdocs.yml:142-146 enables.
//     The passkey deposit: ui/src/x402.js:187-208, one user operation of
//     approve + depositFor(token, device payment key, amount).
//   - Backups: Aurora's automated retention is 7 days (infra/aurora.tf:94).
//     The manual Aurora snapshots were deleted on 2026-10-01 and the EC2
//     server's EBS snapshot on 2026-10-02; verified 2026-10-03: 0 manual
//     cluster snapshots, 0 EBS snapshots. The automated backups are the only
//     copies, so the page names no other.
//   - Not offered on the site today, so not described as running: vault
//     deployment and marketplace publishing (their UI is behind
//     ROADMAP_SURFACES_ENABLED, a UI build flag), on-chain trace publication
//     (no UI; POST /api/traces/publish needs the internal agent key), IPFS
//     pinning (docs/adr/ipfs-pinning-not-live.md), Google Fonts on this site
//     (removed from ui/index.html in the rebrand). The live agent runner runs
//     in dry-run.
//   - Not offered through the API either (#1927). POST /api/vaults/create,
//     POST /api/marketplace/publish and POST /api/marketplace/subscribe (the
//     three routes that have our signer deploy a vault owned by the caller's
//     wallet) depend on require_roadmap_surfaces
//     (api/vaults_routes.py:362, api/marketplace_routes.py:98 and :330). The
//     fourth, POST /api/vaults/metadata, which for a vault the caller
//     deployed outside the site stores public metadata and has our signer
//     anchor its strategies on-chain, is gated the same way by the companion
//     PR dbrowneup/1432-gate-vault-metadata. On this branch alone it is still
//     open, so #1432 must merge after that PR or together with it. The gate
//     answers 404 before auth, body-schema validation or the handler
//     runs unless FEATURE_ROADMAP_SURFACES is "true" (any case, surrounding
//     space ignored; backend/archimedes/feature_flags.py:74, :95). Only
//     middleware and FastAPI's JSON body parse run first (a malformed JSON
//     body gets 422/400, :84-92). It is off by default and
//     unset in every container of the live task definition
//     (archimedes-backend:285, checked 2026-10-03). If it is ever switched
//     on, the blockchain section must say what a vault deployment publishes
//     before it is (promised on the page).
//   - Per-vault chat was deleted on 2026-08-31 and no code on main writes a
//     chat_messages row (models/chat.py:10; the only other references are
//     init_db()'s ALTER TABLE patch, db.py:377, and the read-only count
//     script). Alembic 5cdd4f098a74 (#1926), which deletes the rows and keeps
//     the table, ran in prod on 2026-10-03 and found 0 rows to delete; the
//     re-count printed chat_messages_rows=0. So the page no longer mentions
//     the chat.
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
					Archimedes is a research tool. We collect what we need to run your
					account, generate strategies, take payments, count visitors and stop
					abuse. There are no advertising trackers or third-party analytics
					scripts on this site. We do not sell your data.
				</p>
				<p>
					Three things are less comfortable, so we say them first: we store your
					IP address on each sign-in session and in our server logs; our server
					sets a long-lived visitor cookie even if you decline analytics; and
					deleting your account does not remove your payment records. Each is
					explained below.
				</p>
			</section>

			<section>
				<h2>What we collect when you create an account</h2>
				<ul>
					<li>
						<strong>Your name and email address.</strong> The emails we send are
						described in the next section.
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
						<strong>A record of each sign-in session:</strong> a session token,
						when it expires, and the IP address and browser user-agent of the
						sign-in that created it. For an IPv6 connection we store only the
						first half of the address, the part that identifies your network. A
						session expires seven days after it was last renewed, and using the
						site renews it at most once a day. A job that runs every hour deletes
						expired session records. Account Settings lists your sessions, if you
						signed in within the last 24 hours, and lets you end any of them.
					</li>
					<li>
						<strong>API keys, if you create any.</strong> Keys are created
						through our API, not the site. We store each key&rsquo;s name, a
						salted hash of it (never the key itself), and when it was created,
						last used and revoked. A revoked key stays on record, marked revoked,
						until your account is deleted.
					</li>
				</ul>
				<p>
					Our API also accepts an optional profile attached to a linked wallet: a
					display name, research interests, how you heard about us, a &ldquo;keep
					me updated&rdquo; choice and a separate contact email. The site does
					not currently show a form for it. If you do set one, the contact email
					is encrypted before it is stored, only you can read the profile back,
					and nothing sends email based on the &ldquo;keep me updated&rdquo;
					choice.
				</p>
			</section>

			<section>
				<h2>Emails we send</h2>
				<p>
					Our sign-in service emails you only about your account: to verify your
					address, to reset your password, to confirm a change of email address,
					and to warn you when a sign-in method is added to or removed from your
					account. We do not send marketing email. Our emails are plain text,
					with no open or click tracking. If you write to our privacy address,
					our answers are sent from the operator&rsquo;s Gmail inbox, including
					the message that confirms a request comes from your account and any
					copy of your data; they are not part of the record described next.
				</p>
				<p>
					We keep a record of each of those emails: the address it went to,
					which kind it was, when it was sent, and whether Amazon SES (which
					sends them) accepted it, with SES&rsquo;s message id or, if sending
					failed, the name of the error. The record never includes the message
					or the link in it. These records do not expire, and deleting your
					account erases them.
				</p>
				<p>
					If an email to you bounces permanently or is reported as spam, Amazon
					SES puts your address on a do-not-send list for our account. That list
					is kept by SES, not in our database, so deleting your account does not
					remove your address from it; it stays until we remove it by hand.
				</p>
			</section>

			<section>
				<h2>If you sign in with Google or GitHub</h2>
				<p>
					We ask each provider only for its default sign-in scopes: from Google,
					your email address, name and avatar; from GitHub, read access to your
					profile and your email addresses. We do not request access to your
					files, repositories or contacts.
				</p>
				<p>
					We store the provider&rsquo;s identifier for your account, the scope it
					granted, and the access and refresh tokens it issued, which are
					encrypted before they are stored. We do not store the ID token Google
					issues at sign-in (a signed copy of your Google account details), and
					any stored before this change have been cleared.
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
					Circle&rsquo;s id for it if it is a Circle wallet, and when it was
					verified. A wallet already linked to another account cannot be taken
					over by linking it again; that request is refused.
				</p>
				<p>
					For each link attempt we also keep the challenge you were asked to
					sign. Its one-time code is stored only as a hash. The rest (the
					address, chain, wallet software, Circle&rsquo;s id for the wallet if it
					is a Circle wallet, our site&rsquo;s address, and when it was issued,
					expires and was used) is kept as plain text, whether or not the link
					completed, until your account is deleted.
				</p>
				<p>
					We also keep a ledger keyed by wallet address: when an address was
					first linked here, and events it took part in, such as a generation
					started or completed. Entries in the ledger do not include the
					anonymous visitor id described below. The ledger has no database link
					to your account, so deleting your account does not remove it.
				</p>
				<p>
					Paying from a Circle passkey wallet also links that browser&rsquo;s
					device payment key (see the next section) to your account, and it is
					listed in Account Settings. Once it has paid for a generation that
					produced a strategy, it can no longer be unlinked.
				</p>
			</section>

			<section>
				<h2>Free generations and payments</h2>
				<p>
					Each account gets three free generations once its email address is
					verified, with no wallet and no payment. Each one is recorded in{" "}
					<code>free_generation_grants</code> with your account id, which run
					used it, and when. A free run that fails to produce a strategy is
					handed back, unless our server, database or cache fails along the way.
					That record has no database link to your account, so
					deleting your account does not remove it.
				</p>
				<p>
					Beyond the free allowance, a generation currently costs $2.00 in
					testnet USDC, and the payment really settles. It works through
					Circle&rsquo;s Gateway:
				</p>
				<ul>
					<li>
						Whenever your Gateway balance is too low for a payment (or cannot be
						read), the payment starts with a deposit. Your wallet approves and
						deposits test USDC (20 by default; you can change the amount) into
						Circle&rsquo;s Gateway contract, where it is held as a balance for
						your address.
					</li>
					<li>
						With a Circle passkey wallet, the deposit is instead credited to a
						device payment key: a signing key that your browser generates and
						keeps, unencrypted, in its local storage. Anything that can read that
						storage can spend what is left of the deposit, which is why each
						deposit to the key is capped at $50. If that browser&rsquo;s site
						data is cleared, whatever is left under that key can no longer be
						spent from the site.
					</li>
					<li>
						For each payment, a payment authorisation is signed by a wallet
						linked to your account (with a passkey wallet, the device payment key
						signs it without a prompt). Our server sends it to Circle&rsquo;s
						payment service, which verifies it and moves $2.00 from your Gateway
						balance to ours.
					</li>
				</ul>
				<p>
					When a payment settles, we write a <code>payment_receipts</code> row
					at that moment (the one exception is described below). Each row
					holds:
				</p>
				<ul>
					<li>your account id, and the wallet address that paid;</li>
					<li>
						the amount and the price it was charged at, and the network it was
						charged on;
					</li>
					<li>
						the settlement reference Circle returned — an identifier for the
						transfer, not an on-chain transaction hash;
					</li>
					<li>
						when the payment settled and, once the generation is queued, which
						generation it paid for.
					</li>
				</ul>
				<p>
					A second record, <code>generation_credits</code>, decides what you are
					owed. A settled payment buys a credit and a generation spends it, so a
					run that takes your money and then fails leaves the credit with you,
					unless our server, database or cache fails along the way. A
					credit row holds the payment details above, its state (claimed,
					available, spent or voided), when that changed, which generation spent
					it, and the idempotency key your client sent, if it sent one, which is
					how a retried request is recognised as the same charge.
				</p>
				<p>
					Receipts and credits belong to your account, and no other account can
					read them. After a payment completes, the Generate page shows the
					settlement receipt Circle returned when you go back from the
					run&rsquo;s live view. Your full receipt history is not yet shown on
					the site, but your signed-in account can read it from our API, or you
					can ask us for it. Writing the receipt is never allowed to block the
					generation you paid for, so if our database fails at that moment a
					payment can settle without a receipt row. A missing receipt is a gap
					in the record, not evidence that no charge happened.
				</p>
				<p>
					These records have no expiry date, and deleting your account does not
					remove them: they carry your account id but no database link to the
					account. Account Settings says so before you confirm a deletion.
				</p>
			</section>

			<section>
				<h2>What you make on Archimedes</h2>
				<p>
					We store what you put in and what comes out: the brief you write, the
					strategy generated from it, the papers it drew on, its backtest results
					and rigor verdicts, and the debate between models that produced it. If
					you start a paper-trading deployment, we store its simulated positions,
					trades and daily returns. This is the product, kept so you can come
					back to it. Your strategies and paper-trading records are visible only
					to your account; nothing on the site publishes them.
				</p>
				<p>
					When a generation run gets as far as saving its strategy and
					backtesting it, we also keep each candidate it considered, including
					the ones that fail the rigor gate and the alternates ranked below the
					winner, with your brief, the strategy specification and its verdict. A
					run that ends before that point leaves no candidate records. If it
					produced no strategy, its brief stays only in a job record in our
					cache, deleted an hour after the run ends.
				</p>
				<p>
					For each run that produces a strategy we also record what it consumed
					(token counts per model, elapsed and processor time, peak memory) next
					to the price quote in force when it started. A check runs over the
					measurement before it is saved and raises an error if anything
					price-shaped is in it, so the measurement cannot quietly become a bill.
					Your brief and the model&rsquo;s response text are in neither.
				</p>
				<p>
					<strong>Your brief is sent to a language model to be answered.</strong>{" "}
					Every model we offer is called through Amazon Bedrock under our own AWS
					account, including third-party models such as Meta&rsquo;s Llama or
					DeepSeek; we do not send your brief to any other AI provider. The
					default model runs in AWS&rsquo;s US East region. Two of the models you
					can pick are served through Bedrock&rsquo;s routing across AWS&rsquo;s
					US regions, so with those your brief may be processed in US East or US
					West. Bedrock&rsquo;s own prompt logging is switched off in our
					account.
				</p>
			</section>

			<section>
				<h2>Counting visitors</h2>
				<p>
					We count how many people reach the site and how far they get, mostly
					with counters rather than records of individuals:
				</p>
				<ul>
					<li>
						The first time a page calls our server&rsquo;s API (every page does
						as it loads), the server gives your browser a random, opaque id in a
						cookie named <code>archimedes_vid</code>. It is not derived from your
						IP address, your device or anything about you. It lasts 180 days and
						cannot be read by JavaScript. The server sets it
						whether or not you allow analytics.
					</li>
					<li>
						That id is fed into probabilistic distinct-count sketches
						(HyperLogLog), which count how many different visitors reached each
						step but cannot be read back as a list of ids. Some steps are counted
						by our server whatever you choose in the consent banner: being asked
						to connect a wallet, and starting a generation (and whether it was a
						free one). Your browser reports that you landed on the site only if
						you allow analytics.
					</li>
					<li>
						When that landing report is sent, we also count your country and
						device class, once per visitor. To count you only once, we keep a
						marker holding your visitor id, which expires 180 days after it is
						written. An older list of ids, recorded before this change in October
						2026, expires 180 days after the first landing counted since the
						change.
					</li>
					<li>
						Country comes from a two-letter code that our CDN adds to the
						request; we do not run an IP-geolocation lookup. Device class
						(mobile, tablet, desktop) comes from the same CDN headers, falling
						back to a coarse read of your browser&rsquo;s user-agent, and only
						the bucket is kept.
					</li>
				</ul>
				<p>
					This counting never reads your IP address. Daily counts expire after
					90 days; running totals do not expire. We do not link the visitor id
					to your account or your wallet.
				</p>
			</section>

			<section>
				<h2>IP addresses and logs</h2>
				<p>
					Because the counting above never reads IP addresses, it is easy to miss
					where they are used:
				</p>
				<ul>
					<li>
						Your IP address is stored on each sign-in session record, as
						described above.
					</li>
					<li>
						It is the key for our rate limits and for the daily cap on
						generation requests per address. For IPv6, the key is the first half
						of the address rather than the whole of it. The API rate-limit
						counters and the daily cap live in our cache and expire on their own,
						the counters within an hour and the cap within 36 hours. Our sign-in
						service also counts every request it receives against your IP
						address in a database table, which is how the limits on signing in,
						signing up, password resets and verification emails work; each row
						is deleted a minute or more after its last request, when a later
						request triggers the clean-up.
					</li>
					<li>
						Amazon&rsquo;s web application firewall rate-limits requests per IP
						address at our CDN, and a second firewall at our load balancer
						screens requests with Amazon&rsquo;s managed rules. We keep no
						firewall logs; AWS keeps a small sample of recent requests,
						including their IP addresses, for up to three hours.
					</li>
					<li>
						Our web server logs every request: your IP address, browser
						user-agent, the address requested, the page that linked to it (the
						referrer), and the chain of forwarding addresses. Sign-in tokens
						are removed from that access log, though
						not from the error entry written when a request fails inside our
						servers. Our application keeps a similar log of API requests, with
						your IP address and the address requested; some of those addresses
						contain a wallet address. Both are kept for 90 days.
					</li>
					<li>
						Our load balancer logs every request with your browser user-agent and
						the full address requested, including the one-time tokens in
						email-verification and password-reset links (a verification token
						contains your email address in encoded form). For visits through our CDN
						it records the CDN server&rsquo;s IP address rather than yours. These
						logs are deleted after 30 days.
					</li>
				</ul>
				<p>
					We use this to keep accounts secure, to stop one person from draining
					a shared resource, and to diagnose problems. None of it is used to
					profile you or shared with advertisers.
				</p>
			</section>

			<section>
				<h2>Cookies and browser storage</h2>
				<p>
					On your first visit a banner asks whether to allow two optional kinds
					of browser storage, functional and analytics. Both stay off until you
					choose, and you can change your choice on our{" "}
					<a href="/security#storage-disclosure">Security page</a>. The banner
					controls only what your browser stores and reports; it does not stop
					the cookies our server sets.
				</p>
				<p>
					Cookies, all set by our own servers, hidden from JavaScript, and sent
					only over HTTPS:
				</p>
				<ul>
					<li>
						<strong>The sign-in session cookie,</strong> present once you sign in.
						It expires with the session, seven days after it was last renewed.
					</li>
					<li>
						<strong>A five-minute sign-in check,</strong> set only while a Google
						or GitHub sign-in is in progress, so that the sign-in that comes back
						is the one you started.
					</li>
					<li>
						<strong>
							<code>archimedes_vid</code>
						</strong>
						, the visitor id described above: 180 days, set whatever you choose
						in the banner.
					</li>
				</ul>
				<p>
					Your browser&rsquo;s own storage, which stays on your device unless
					you send it to us:
				</p>
				<ul>
					<li>
						<strong>Always, because the site needs them:</strong> which wallet you
						last connected, your consent choice, short-lived per-tab markers used
						while linking a sign-in method or using a passkey, and, if you use a
						Circle passkey wallet, that wallet&rsquo;s credential and its device
						payment key. The payment key is a private key, stored unencrypted,
						that can spend whatever is left of what you deposited to it.
					</li>
					<li>
						<strong>Only if you allow functional storage:</strong> preferences
						such as your light or dark theme, a nickname for your wallet, whether
						you finished the onboarding tour, and your rigor-strictness setting.
					</li>
					<li>
						<strong>Only if you allow analytics:</strong> a per-tab marker so that
						your landing is reported once.
					</li>
				</ul>
				<p>
					None of these is an advertising or cross-site tracking identifier. The
					Security page lists every cookie and storage key by name, with what
					each one reveals.
				</p>
			</section>

			<section>
				<h2>What goes on a public blockchain</h2>
				<p>
					Archimedes runs on the Arc public testnet.{" "}
					<strong>On-chain records are public and permanent:</strong> anyone can
					read them, and nobody, including us, can edit or delete them. A
					deletion request reaches our database; it cannot reach a blockchain.
				</p>
				<p>
					Through the site, the on-chain records you create are your
					wallet&rsquo;s transactions funding Circle&rsquo;s Gateway: a token
					approval and a deposit. With a Circle passkey wallet the two go as one
					operation whose deposit names this browser&rsquo;s device payment key,
					and the first one also deploys the wallet&rsquo;s smart-contract
					account. Circle then settles payments made from that
					balance on-chain, on its own schedule. Paper trading is simulated and
					writes nothing to a chain. The site does not currently offer vault
					deployment, on-chain publication of reasoning traces or marketplace
					publishing, and we do not pin anything to IPFS; if that changes, this
					page will say what gets published before the feature is switched on.
				</p>
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
						<strong>Amazon Web Services.</strong> Our servers, database, cache and
						logs run in AWS&rsquo;s US East (N. Virginia) region, and every
						request to the site passes through Amazon CloudFront, AWS&rsquo;s
						global content-delivery network, on the way. AWS therefore holds
						almost everything this page describes us storing.
					</li>
					<li>
						<strong>Amazon SES,</strong> which sends the account emails described
						above and receives mail sent to privacy@archimedes-arc.com. It sees
						your email address and the contents of those messages, and keeps the
						do-not-send list described above.
					</li>
					<li>
						<strong>Amazon Bedrock,</strong> which runs the language models. Your
						brief and the context around it are sent to it as a prompt, as
						described above.
					</li>
					<li>
						<strong>Google or GitHub,</strong> only if you choose to sign in with
						them, and only for what that sign-in requires.
					</li>
					<li>
						<strong>Google (Gmail),</strong> if you email
						privacy@archimedes-arc.com: that address forwards to the
						operator&rsquo;s personal Gmail inbox.
					</li>
					<li>
						<strong>Circle.</strong> When you pay for a generation, the paying
						wallet address and the authorisation you signed go to Circle&rsquo;s
						payment service, whatever wallet you use. If you create a Circle
						passkey wallet, your browser registers a public key and a username
						with Circle (the wallet name you type, or a generated one if you
						leave it blank), and then sends that wallet&rsquo;s transactions
						through Circle, which pays their network fees. We never send Circle
						your email address or password.
					</li>
					<li>
						<strong>The Arc testnet network.</strong> Your browser talks directly
						to the Arc testnet RPC endpoint and, when you follow a transaction
						link, to the Arc block explorer. Those services see your IP address
						and what you asked the chain about.
					</li>
					<li>
						<strong>Our documentation site,</strong> docs.archimedes-arc.com,
						linked from the header and footer of our public pages. It
						loads its typefaces from Google Fonts, asks GitHub&rsquo;s API for our
						repository&rsquo;s details and, on pages with diagrams, loads a
						script from unpkg.com, all from your browser, so Google, GitHub and
						unpkg see your IP address when you open it.
					</li>
				</ul>
				<p>
					This site itself serves its typefaces from our own domain and makes no
					requests to Google Fonts or any other font service. Market price data
					is pulled by our servers from public market-data sources; that is a
					one-way request we make, and nothing about you goes with it.
				</p>
			</section>

			<section>
				<h2>What we do not do</h2>
				<ul>
					<li>
						<strong>No advertising or analytics trackers.</strong> There is no
						Google Analytics, no Segment, no Mixpanel, no PostHog, no Facebook
						pixel, and no error-reporting service on this site. Our content
						security policy also blocks the browser from running scripts from any
						other site, so a third-party tracking script could not be added by
						accident.
					</li>
					<li>
						<strong>We do not sell your data,</strong> and we do not share it for
						advertising.
					</li>
					<li>
						<strong>We do not buy or import contact lists.</strong> The only
						addresses we email are ones people gave us themselves: typed into our
						forms, supplied by the Google or GitHub sign-in they chose, or used to
						write to us.
					</li>
				</ul>
			</section>

			<section>
				<h2>How long we keep things, and how to get them deleted</h2>
				<p>Some things expire on their own:</p>
				<ul>
					<li>
						Sign-in sessions, seven days after they were last renewed; an hourly
						job then deletes the expired records.
					</li>
					<li>
						API rate-limit counters, within an hour; daily generation caps, within
						36 hours; generation job records in our cache, an hour after the run
						ends.
					</li>
					<li>
						Visitor-id markers, 180 days after they are written; daily visitor
						counts, after 90 days.
					</li>
					<li>
						Firewall samples, within three hours; load balancer logs, after 30
						days; web server and application logs, after 90 days.
					</li>
					<li>Automated database backups, after 7 days.</li>
				</ul>
				<p>
					Everything else has no expiry date today: your account and profile,
					your strategies and every stored generation candidate, your
					paper-trading records, your payment receipts and credits, your
					free-generation record, the wallet ledger, the email log, messages you
					send to our privacy address and our replies, including any copy of
					your data we send (they stay in the operator&rsquo;s Gmail inbox), API
					keys, wallet-link challenges, and running visitor totals.
				</p>
				<p>
					<strong>You can delete your account yourself</strong> in Account
					Settings, under Delete account. You type a confirmation, plus your
					password if your account has one (otherwise you must have signed in
					within the last day), and the deletion takes effect at once, with no
					recovery window. Apart from the expiries listed above, nothing deletes
					your data automatically. What account deletion does, exactly:
				</p>
				<ul>
					<li>
						<strong>Erased:</strong> your sessions, your API keys, your sign-in
						methods (your password and any linked Google or GitHub), your wallet
						links and wallet-link challenges, your profile row
						(which holds your encrypted contact email), your paper-trading
						deployments with everything recorded under them in our database, and
						the log of emails we sent you.
					</li>
					<li>
						<strong>Detached from you rather than destroyed:</strong> your
						strategies, their rigor passports, the generation records behind
						them, and descriptions of vaults you created stay in our database
						with your account id removed, because other accounts can reference
						them. They still contain your brief text, any strategy or vault name
						you typed and, if a wallet was linked when they were made, that
						wallet&rsquo;s address. If that wallet is later linked to an account
						again, the records that carry it show up under that account.
					</li>
					<li>
						<strong>Not touched by account deletion:</strong> your payment
						receipts, your credit ledger (an unspent credit can no longer be
						used), your free-generation record and the wallet ledger, because none
						of them has a database link to your account; the reasoning record of
						each paper-trading decision, which we keep in our cache with your
						account id and wallet address, if any; and nothing on a blockchain.
					</li>
				</ul>
				<p>
					<strong>
						On request, we will erase or scrub what account deletion leaves, send
						you a copy, or both.
					</strong>{" "}
					Write to the contact address below. If you ask us to erase your data, we
					will delete your account if it still exists, erase your payment
					receipts, credit ledger (which gives up any unspent credit),
					free-generation record, the reasoning records of your paper-trading
					decisions and your wallet&rsquo;s entries in the wallet ledger, and
					remove your brief text, any strategy or vault name you typed, and your
					wallet address from the records that were detached from you. If you ask
					for a copy, we will send you a copy of the records we hold about your
					account or wallet in our database, other than password hashes and
					sign-in tokens. There is no self-service export yet; these requests are
					handled by hand, and before we erase or send anything we will ask you to
					show that the account or wallet is yours: for the account, by answering
					a message we send to its email address; for a wallet, by signing a
					message with it (for a Circle passkey wallet, by linking it to an
					account here, which signs our challenge). That message moves no funds
					and approves nothing, and we will never ask you to sign a transaction to
					prove a request. While the account exists, a wallet signature covers
					only that wallet&rsquo;s records, and a copy of account records goes
					only to the account&rsquo;s email address. We never ask for identity
					documents. Write before you delete your account yourself, or ask us to
					delete it as part of the request: once it is deleted, we can find what
					it left only through a wallet you linked or paid with. We can then erase
					what it left, but we can send a copy only of that wallet&rsquo;s
					records, because there is no longer an account email to send the rest
					to. Write the same way if you cannot sign in to delete your account
					yourself.
				</p>
				<p>Limits, stated up front:</p>
				<ul>
					<li>Anything on a blockchain cannot be deleted by anyone (see above).</li>
					<li>
						Log entries age out on the schedules above rather than being removed
						one account at a time.
					</li>
					<li>
						Deleted data remains in our automated database backups for up to 7
						days.
					</li>
					<li>
						An address on the SES do-not-send list stays there until we remove it
						by hand.
					</li>
					<li>
						Our correspondence with you, including any copy of your data we send,
						stays in the operator&rsquo;s Gmail inbox (see above).
					</li>
					<li>
						We cannot erase what Circle, Google or GitHub keep about you; see
						&ldquo;Who else is in the path&rdquo;.
					</li>
				</ul>
			</section>

			<section>
				<h2>Security</h2>
				<p>
					Passwords are hashed. Sign-in provider access and refresh tokens, and
					the optional contact email, are encrypted before storage. Our cookies
					are hidden from JavaScript and sent only over HTTPS. Sign-in and
					sign-up are rate limited. Profile answers and contact emails are
					redacted from our application logs, and sign-in tokens from our web
					server&rsquo;s access log, but account ids, wallet addresses, IP
					addresses and browser user-agents do appear in our logs, which expire
					on the schedules above.
				</p>
				<p>
					Archimedes runs on a public testnet and is early software. Please do
					not connect a wallet holding assets you care about, do not deposit
					more test USDC than you need, and do not put anything in a brief that
					you would not want stored.
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
					We will update this page whenever what the software does changes. It is
					still a draft awaiting the owner&rsquo;s approval and can lag behind the
					code; the issue tracker below is the place to point out where it does.
					Once the page is approved, the &ldquo;last updated&rdquo; line at the
					top will record each change; until then it is undated. If a change
					materially affects what we collect or who receives it, we will say so
					on this page rather than editing quietly.
				</p>
			</section>

			<section>
				<h2>Contact</h2>
				<p>
					Privacy questions, and requests to delete or get a copy of your data,
					go to{" "}
					<a href="mailto:privacy@archimedes-arc.com">
						privacy@archimedes-arc.com
					</a>
					. Mail to that address is received by Amazon SES and forwarded through
					Amazon SNS to the operator&rsquo;s personal Gmail inbox; we keep no
					other copy of it. Messages larger than 150&nbsp;KB, such as ones with
					attachments, bounce, so please send text only and include only the
					personal details your request needs.
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
