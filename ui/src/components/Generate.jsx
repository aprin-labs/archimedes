import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import GenerationStream from "./GenerationStream";
import GenerationStatus from "./GenerationStatus";
import ModelCostPanel from "./ModelCostPanel";
import FreeGenerationBanner from "./FreeGenerationBanner";
import ResendVerificationControl from "./ResendVerificationControl";
import { SURPRISE_BRIEFS } from "../data/surpriseBriefs";
import { pickSurpriseBrief } from "../data/pickSurpriseBrief";
import { ASSET_GROUPS, SUPPORTED_ASSETS } from "../data/assetUniverse";
import { apiGet, apiPostWithMeta } from "../api";
import { deriveWalletGateView } from "../freeGenerations";
import { getAddress } from "../config";
import { listLinkedWallets } from "../linked-wallets";
import { GENERATION_QUOTE_ENABLED } from "../featureFlags";
import { depositToGateway, getGatewayBalance, parseUsdcAmount, paymentPayerAddress, paymentWalletKind, signGatewayPayment, walletSupportsPayment } from "../x402";
import { ensureSessionLinked, getOrCreateSessionAccount } from "../payment-session";
import {
	DEFAULT_DEPTH,
	DEPTH_OPTIONS,
	PAYMENT_STATUS,
	deriveQuoteView,
	derivePaymentRequirements,
	derivePaymentState,
	describePayerMismatch,
	extractPaymentRequiredHeader,
	extractReceipt,
	paymentErrorMessage,
	startErrorMessage,
} from "../generateQuote";

const shortAddr = (addr) => (addr ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : "");

// Engine C (fusion) per-spec asset fan-out cap — mirrors `_MAX_ASSETS` in
// backend/archimedes/services/fusion_market_data.py:53. Deeper selections are
// truncated LOUDLY at generation time (logged + provenance-visible), never
// silently. Not derived at build time (cross-language constant, JS can't
// import it) — keep this in sync if the backend value changes; grep
// `_MAX_ASSETS` there to re-verify.
const ENGINE_C_ASSET_CAP = 6;

// Mirrors GenerateBrief.intent's server-side max_length (INTENT_MAX_LEN,
// generate_schemas.py, #1801). Cross-language constant, held together by
// ui/test/generate-brief-limits.test.js.
const BRIEF_MAX_LEN = 600;

// /generate spine page — redesigned per issue #872, re-laid-out per #1642.
//
// Layout (top to bottom):
//   1. Model selector (ModelCostPanel)
//   2. Brief input + submit
//   3. Recent Generations job table
//
// MOBILE-FIRST (#1642). The phone layout is the base stylesheet, not a
// media-query afterthought: `.generate-workbench` is a single column at
// every width by default and only becomes the two-column brief+rail grid at
// `min-width: 900px` (App.css, the "#1642 — Generate, mobile-first" block at
// the end of the file). Consequence for anyone editing the markup: source
// order IS the phone order. The brief card is first because it is the thing
// the page is for; the context rail (model picker + pipeline-inputs note)
// follows it, because on a 375px screen a rail above the input would push
// the input below the fold.
//
// "Surprise me" (#1642) replaced the always-visible three-entry
// "Examples — click to fill" list. The bank is ../data/surpriseBriefs.js
// (124 entries) and NOTHING from it renders until the button is pressed —
// no preview, no fallback list on error, nothing at mount. The picked
// entry goes straight into the textarea; the picker
// (../data/pickSurpriseBrief.js) is passed the previous pick's id so two
// presses in a row can never return the same brief.
//
// Submitting queues a job and adds a row to the table; the user STAYS on
// the page. Clicking a row opens that job's live SSE stream as a drill-down
// view. The SSE endpoint honours Last-Event-ID for replay, so re-subscribing
// on drill-in shows the full history for a completed job too.
//
// Upfront cost quote + x402 paywall (feature-flagged, GENERATION_QUOTE_ENABLED
// in featureFlags.js — ON by default since Dan's 2026-08-19 directive):
// when on, a quote is fetched from the PUBLIC GET /api/generate/quote and
// shown before the submit button. /start's request body is unchanged — the
// ratified contract (docs/specs/generation-quote-contract.md, #1296) carries
// no quote_id or any other payment field on the body; the gate lives
// entirely in status codes: a 409 means "link + fund a wallet first" (CTA
// reuses the existing open-wallet-modal flow), a 402 means "sign and retry
// with Payment-Signature".
//
// The 402 path handling works REGARDLESS of GENERATION_QUOTE_ENABLED — a
// live paywall can 402 an unquoted submit just as easily as a quoted one, so
// the payment step below reads its own price/wallet info from the 402's
// body/headers rather than assuming the upfront quote ran.
//
// Real x402 signing IS wired (../x402.js): the 402's PAYMENT-REQUIRED
// header is parsed (../generateQuote.js's derivePaymentRequirements) into
// EIP-712 payment requirements; if the connected Gateway balance is short,
// a two-transaction approve+deposit flow funds it; then the connected
// wallet signs a TransferWithAuthorization and /start is retried with the
// resulting Payment-Signature header. A real signed header is ALSO accepted
// under the backend's PAYMENTS_DRY_RUN (unverified, unsettled — never
// dressed up as a real payment), so this one flow works whether the backend
// is live or still in dry-run. A settlement receipt (PAYMENT-RESPONSE
// header) is surfaced when present; when a retry succeeds WITHOUT one, the
// honest "accepted without settlement" notice renders instead — this UI
// never claims a payment succeeded without a receipt. State-transition
// logic and the x402 wire-format parsing/building live in
// ../generateQuote.js so they're unit-testable without a DOM or a wallet.

// "$2.000000" reads like a debug dump — show money as money ("$2.00").
// Server strings stay authoritative; this only trims display precision.
const fmtUsd = (p) => (typeof p === "string" ? p.replace(/^(\$\d+\.\d{2})\d*$/, "$1") : p);

const RISK_PROFILES = [
	{ id: "fixed_income", label: "Fixed income" },
	{ id: "conservative", label: "Conservative" },
	{ id: "moderate", label: "Moderate" },
	{ id: "aggressive", label: "Aggressive" },
	{ id: "hyper_risky", label: "Hyper-risky" },
];

// ─────────────────────────────────────────────────────────────

export default function Generate({ onNavigate, onStageChange, user }) {
	// ── Brief form state ──
	const [intent, setIntent] = useState("");
	const [starting, setStarting] = useState(false);
	const [startError, setStartError] = useState("");

	// ── Model selector (lifted; passed to ModelCostPanel) ──
	const [selectedModel, setSelectedModel] = useState(null);

	// ── Advanced options (hidden by default) ──
	const [advancedOpen, setAdvancedOpen] = useState(false);
	const [riskAppetite, setRiskAppetite] = useState("moderate");
	const [depth, setDepth] = useState(DEFAULT_DEPTH);
	const [selectedAssets, setSelectedAssets] = useState([]);
	const [assetQuery, setAssetQuery] = useState("");
	const [strategyName, setStrategyName] = useState("");

	// ── Surprise Me (#1642) ──
	// Holds ONLY the id of the last pick, never the brief text: the text lives
	// in `intent` (the textarea) once applied, so there is no second place a
	// bank entry could leak onto the screen before the button is pressed.
	const [lastSurpriseId, setLastSurpriseId] = useState(null);
	const [surpriseLabel, setSurpriseLabel] = useState("");

	// ── Drill-down: which job's stream to show (null = table view) ──
	const [drillInJobId, setDrillInJobId] = useState(null);
	// Focus handoff for the "ways forward" on a failed run: bumping the
	// counter moves the caret into the brief box after the stream view
	// unmounts, so the user lands where the edit actually happens.
	const [focusBriefTick, setFocusBriefTick] = useState(0);
	const briefRef = useRef(null);

	useEffect(() => {
		onStageChange?.(drillInJobId ? "debate" : "brief");
	}, [drillInJobId, onStageChange]);

	// ── Track the most-recently submitted job so the table can highlight it ──
	const [lastJobId, setLastJobId] = useState(null);

	// ── Upfront cost quote + x402 paywall (feature-flagged) ──
	const [quote, setQuote] = useState(null);
	const [quoteStatus, setQuoteStatus] = useState(
		GENERATION_QUOTE_ENABLED ? "loading" : "idle",
	);
	const [quoteError, setQuoteError] = useState("");
	const [walletAddr, setWalletAddr] = useState(() => getAddress());
	// The account's linked wallets (GET /api/wallets) — fetched once a payment
	// step is actually active (a 409/402 landed), regardless of
	// GENERATION_QUOTE_ENABLED, so the 402 handling path works whether or not
	// the upfront quote ran. Used for the payer-binding transparency note
	// (describePayerMismatch) — real signing always uses the CONNECTED wallet
	// (walletAddr) as the payer, since that's the only address this UI can
	// actually produce a signature for; a linked wallet the user isn't
	// connected as can only be flagged, never silently substituted.
	const [linkedWallets, setLinkedWallets] = useState([]);
	// The payment-step banner. Set only from a genuine 409/402 on /start
	// (derivePaymentState); cleared on every fresh submit attempt so a stale
	// banner never survives a retry.
	const [paymentStatus, setPaymentStatus] = useState(PAYMENT_STATUS.NONE);
	const [paymentMessage, setPaymentMessage] = useState("");
	// Human view of a 409 wallet_link_required — derived once from the error
	// so the panel never dumps the agent/curl `detail.message` (#1658 leftover).
	const [walletGateView, setWalletGateView] = useState(null);
	// The parsed PAYMENT-REQUIRED requirements from the active 402 (or an
	// error state if the header was missing/malformed/had no supported
	// option) — see ../generateQuote.js's derivePaymentRequirements. Null
	// until a 402 lands.
	const [paymentRequirements, setPaymentRequirements] = useState(null);
	// When the active requirements were parsed. WebKit refuses a WebAuthn
	// ceremony after any await chain, so the pay tap must sign with HELD
	// requirements, not refetched ones — this timestamp bounds how old a
	// held set may be before we refetch anyway (the server window is 7
	// days, #1452; the cap only guards a config change under an idle tab).
	const [paymentRequirementsAt, setPaymentRequirementsAt] = useState(0);
	// The 402 body's OWN quote ({price, asset, chain, dry_run, ...}) —
	// captured independently of the upfront GET /api/generate/quote fetch so
	// the payment step can show a price even when GENERATION_QUOTE_ENABLED
	// never fetched one.
	const [paywallQuoteRaw, setPaywallQuoteRaw] = useState(null);
	// Gateway balance (raw base-unit bigint) for the connected wallet against
	// the active requirement's asset. Null while unknown/checking.
	const [gatewayBalance, setGatewayBalance] = useState(null);
	// The approve+deposit funding flow — one faucet drip (20 USDC) funds
	// exactly 10 generations at the $2.00 default price, hence the default.
	const [depositAmount, setDepositAmount] = useState("20.00");
	const [depositError, setDepositError] = useState("");
	const [paying, setPaying] = useState(false);
	// True only right after a /start retry succeeded WITHOUT a PAYMENT-RESPONSE
	// settlement receipt (PAYMENTS_DRY_RUN on the backend: a real signed
	// payment header is accepted UNVERIFIED and UNSETTLED). The honest label
	// the ratified contract requires — never dressed up as a real settlement.
	const [noSettlementNotice, setNoSettlementNotice] = useState(false);
	// The PAYMENT-RESPONSE settlement receipt from a successful /start, if
	// the response carried one (only true payments settled — live mode).
	const [receipt, setReceipt] = useState(null);

	// ── The caller's own generation-credit ledger (v8 Lane 1.3a) ──
	// Surfaces what `_paywall_with_credit` already does silently server-side:
	// an unspent ("available") credit from an earlier paid-but-undelivered
	// run pays for the NEXT generation, no new charge (#1441). The FETCH runs
	// independently of GENERATION_QUOTE_ENABLED — a credit is a real balance
	// regardless of whether the upfront quote card renders. The NOTICE below
	// additionally needs the quote's payment_required, so with the quote flag
	// explicitly off (it defaults on, featureFlags.js) the balance is still
	// read but nothing is claimed about it — the honest degraded state here is
	// silence, not an unverified "no new charge".
	const [credits, setCredits] = useState([]);
	const [creditNoticeDismissed, setCreditNoticeDismissed] = useState(false);

	const fetchCredits = useCallback(() => {
		apiGet("/api/generate/credits")
			.then((data) => setCredits(Array.isArray(data) ? data : []))
			.catch(() => {
				// Quiet on failure (e.g. not signed in yet) — this is a
				// nice-to-have notice, not a gate; nothing here may block or
				// error the page.
				setCredits([]);
			});
	}, []);

	const fetchQuote = useCallback(() => {
		if (!GENERATION_QUOTE_ENABLED) return;
		setQuoteStatus("loading");
		setQuoteError("");
		apiGet("/api/generate/quote")
			.then((data) => {
				setQuote(data);
				setQuoteStatus("ready");
			})
			.catch((e) => {
				setQuote(null);
				setQuoteError(e.message || "Cost quote unavailable");
				setQuoteStatus("error");
			});
	}, []);

	useEffect(() => {
		fetchQuote();
	}, [fetchQuote]);

	useEffect(() => {
		fetchCredits();
	}, [fetchCredits]);

	// Wallet connect/disconnect is a global event (config.js), not React
	// state — re-derive our copy on change so the payer-mismatch note and
	// payment-step banner stay live.
	useEffect(() => {
		const onWalletChanged = () => setWalletAddr(getAddress());
		window.addEventListener("wallet-changed", onWalletChanged);
		return () => window.removeEventListener("wallet-changed", onWalletChanged);
	}, []);

	// Payment step active: a genuine 402 landed (dry-run or live — both need
	// the real payment flow now that signing is wired). Computed once here
	// so the linked-wallets fetch, the Gateway-balance fetch, and the render
	// branch below all agree on the same condition.
	const paymentActive =
		paymentStatus === PAYMENT_STATUS.DRY_RUN || paymentStatus === PAYMENT_STATUS.LIVE_UNAVAILABLE;

	// Linked wallets: needed once EITHER the upfront quote reports payments
	// required OR a 402 actually landed — the latter matters independently of
	// GENERATION_QUOTE_ENABLED, since a live paywall can 402 an unquoted
	// submit just as easily as a quoted one, and the payer-mismatch note must
	// still work in that path.
	useEffect(() => {
		if (!paymentActive && !(GENERATION_QUOTE_ENABLED && quote?.payment_required)) return;
		let cancelled = false;
		listLinkedWallets()
			.then((wallets) => {
				if (!cancelled) setLinkedWallets(Array.isArray(wallets) ? wallets : []);
			})
			.catch(() => {
				if (!cancelled) setLinkedWallets([]);
			});
		return () => {
			cancelled = true;
		};
	}, [paymentActive, quote?.payment_required]);

	// A still-spendable credit, if any. Note the route lists NEWEST-first
	// (models/generation_credit.list_credits orders created_at DESC) while the
	// ledger drains OLDEST-first (take_available_credit orders created_at
	// ASC) — so this is NOT necessarily the credit the next submit spends.
	// That's fine: only its PRESENCE drives the notice, never its identity,
	// and the notice must not name or price a specific credit for that reason.
	const unspentCredit = useMemo(() => credits.find((c) => c.status === "available") ?? null, [credits]);

	const quoteView = useMemo(() => deriveQuoteView(quote), [quote]);
	// The price/asset/chain/dry_run to show in the payment step: the upfront
	// quote when it ran, else the 402 body's OWN quote — so the payment step
	// shows a real price regardless of GENERATION_QUOTE_ENABLED. Both
	// useMemo calls run unconditionally (Rules of Hooks) — only the
	// resulting VALUES are chosen with `??`.
	const paywallQuoteView = useMemo(() => deriveQuoteView(paywallQuoteRaw), [paywallQuoteRaw]);
	const effectiveQuoteView = quoteView ?? paywallQuoteView;
	const quoteReady = !GENERATION_QUOTE_ENABLED || (quoteStatus === "ready" && Boolean(quoteView));
	// The wallet actually active in the injected provider vs. the account's
	// LINKED wallet — real signing uses the CONNECTED wallet (it's the only
	// one this UI can produce a signature for), and the backend requires that
	// to equal the linked wallet, so a mismatch here blocks payment rather
	// than silently substituting. Null when there's nothing to flag (see
	// describePayerMismatch's own doc for the exact conditions).
	const payerMismatch = useMemo(
		() => describePayerMismatch(walletAddr, linkedWallets),
		[walletAddr, linkedWallets],
	);

	const requiredAmountRaw = useMemo(() => {
		const amount = paymentRequirements?.requirements?.amount;
		try {
			return amount != null ? BigInt(amount) : null;
		} catch {
			return null;
		}
	}, [paymentRequirements]);
	// Fetch the connected wallet's Gateway balance once a payable requirement
	// is active and the payer is unambiguous (connected + linked agree).
	useEffect(() => {
		const requirements = paymentRequirements?.requirements;
		// The balance that matters is the PAYER's: the connected wallet for
		// EOA kinds, the device payment key for the passkey kind (null until
		// the first payment creates one — the deposit disclosure then shows).
		const payer = paymentPayerAddress();
		if (!requirements || !walletAddr || !payer || payerMismatch) {
			setGatewayBalance(null);
			return;
		}
		let cancelled = false;
		getGatewayBalance(requirements, payer)
			.then((bal) => {
				if (!cancelled) setGatewayBalance(bal);
			})
			.catch(() => {
				if (!cancelled) setGatewayBalance(null);
			});
		return () => {
			cancelled = true;
		};
	}, [paymentRequirements, walletAddr, payerMismatch]);

	// True exactly when the payment panel renders its own pay button — the
	// main submit button hides then, so precisely ONE button is on screen
	// (Dan's 2026-08-21 field report: "Pay & generate" next to "Approve &
	// Generate" is redundant and confusing). Every other panel branch keeps
	// the main button as the retry control after the user fixes the
	// connect/link condition it describes.
	const payPanelReady =
		(paymentStatus === PAYMENT_STATUS.DRY_RUN ||
			paymentStatus === PAYMENT_STATUS.LIVE_UNAVAILABLE) &&
		!paymentRequirements?.error &&
		!!walletAddr &&
		!payerMismatch &&
		walletSupportsPayment();

	// ── Build the /start request body. No payment field on it anywhere —
	// the ratified contract (#1296) gates entirely via status codes/headers,
	// not a request-body field (the PROPOSED contract's quote_id is dead). ──
	const buildBrief = () => ({
		brief: {
			intent,
			risk_appetite: riskAppetite,
			asset_classes: selectedAssets.length > 0 ? selectedAssets : undefined,
			max_papers: depth,
			...(strategyName.trim() ? { name: strategyName.trim() } : {}),
		},
		...(selectedModel ? { model: selectedModel } : {}),
	});

	// One idempotency key per payment ATTEMPT, reused across every /start call
	// inside that attempt (the bare 402 probe, the signed retry, and any user
	// re-click after an ambiguous failure) and regenerated only after a job is
	// actually accepted. The backend's generation-credit ledger (#1498) dedups
	// duplicate charges ONLY when this header is present — the 2026-08-30
	// external review paid twice precisely because the frontend never sent it:
	// each re-click signed a fresh EIP-3009 nonce, which settles as a brand-new
	// payment (generation_credit.py's own docstring calls this out).
	const paymentAttemptKeyRef = useRef(null);
	const paymentAttemptKey = () => {
		if (!paymentAttemptKeyRef.current) {
			paymentAttemptKeyRef.current = crypto.randomUUID();
		}
		return paymentAttemptKeyRef.current;
	};

	// POST /start, optionally with a Payment-Signature header, and surface
	// whatever the response carries (job id + PAYMENT-RESPONSE if present).
	// Returns the receipt too (not just via the `receipt` state setter) so a
	// caller mid-async-flow (handlePay) can branch on it immediately rather
	// than reading stale state before the next render.
	const submitStart = async (extraHeaders) => {
		const { data, headers } = await apiPostWithMeta("/api/generate/start", buildBrief(), {
			"Idempotency-Key": paymentAttemptKey(),
			...extraHeaders,
		});
		setLastJobId(data.job_id);
		const settledReceipt = extractReceipt(headers);
		setReceipt(settledReceipt);
		return { data, receipt: settledReceipt };
	};

	// A job was ACCEPTED (202): consume the attempt key so the next generation
	// is a genuinely new attempt, and take the user straight into the running
	// job's stream. Before this, a successful payment quietly re-armed the
	// "Approve & Generate" button beside a small receipt line — which reads
	// exactly like a failed payment, and is how the double-pay happened.
	const enterStartedJob = (data) => {
		paymentAttemptKeyRef.current = null;
		if (data?.job_id) setDrillInJobId(data.job_id);
	};

	// Reset every payment-step-local piece of state — shared by a fresh
	// submit attempt and a successful payment, so neither leaves stale
	// deposit/balance/error state behind for the next 402.
	const resetPaymentStepState = () => {
		setPaymentStatus(PAYMENT_STATUS.NONE);
		setPaymentMessage("");
		setWalletGateView(null);
		setPaymentRequirements(null);
		setPaymentRequirementsAt(0);
		setPaywallQuoteRaw(null);
		setGatewayBalance(null);
		setDepositError("");
		setNoSettlementNotice(false);
	};

	// ── Submit a generation job ──
	const startJob = async () => {
		setStartError("");
		if (!intent.trim()) {
			setStartError("Describe what you want in a sentence or two.");
			return;
		}
		if (GENERATION_QUOTE_ENABLED && !quoteReady) {
			setStartError("Cost quote isn't ready yet.");
			return;
		}
		setStarting(true);
		resetPaymentStepState();
		setReceipt(null);
		try {
			const { data } = await submitStart();
			// Re-quote for the next generation — flat pricing has nothing to
			// consume, but the flag/dry_run/recipient could change underneath.
			if (GENERATION_QUOTE_ENABLED) fetchQuote();
			// A just-spent credit (or a fresh one this run settled but didn't
			// spend) changes the ledger — refresh regardless of the quote flag.
			fetchCredits();
			// Straight into the running job's stream — "stay on the page and
			// find the new table row" read as a failed start (2026-08-30
			// external review), and the stream IS the product moment.
			enterStartedJob(data);
		} catch (e) {
			const dryRun = e?.detail?.quote?.dry_run ?? quote?.dry_run;
			const state = derivePaymentState(e, dryRun);
			if (state !== PAYMENT_STATUS.NONE) {
				setPaymentStatus(state);
				setPaywallQuoteRaw(e?.detail?.quote ?? null);
				if (state === PAYMENT_STATUS.DRY_RUN || state === PAYMENT_STATUS.LIVE_UNAVAILABLE) {
					// The 402 detail.message is agent/curl guidance ("Sign the
					// PAYMENT-REQUIRED requirements…") — the interactive panel below
					// IS the human version of that instruction; rendering both was
					// pure noise (Dan's 2026-08-21 field report).
					setPaymentRequirements(derivePaymentRequirements(extractPaymentRequiredHeader(e.headers)));
					setPaymentRequirementsAt(Date.now());
				} else {
					// 409 wallet_link_required. Same anti-dump rule as 402: the
					// backend message names POST /api/... paths for agents.
					setWalletGateView(deriveWalletGateView(e));
				}
			} else {
				setStartError(startErrorMessage(e, "Failed to start generation"));
			}
		} finally {
			setStarting(false);
		}
	};

	// One-click pay step ("idle" | "refreshing" | "approving" | "depositing"
	// | "signing" | "starting" | "confirm") — drives the single button's
	// label so the user watches ONE control narrate the whole flow instead
	// of juggling a deposit button and a pay button (Dan's 2026-08-21 field
	// report: "redundant, confusing" — this is the fix). "confirm" = a
	// signature is ready to be requested but the browser needs a fresh tap
	// to open the prompt (WebKit activation rules, or the user cancelled).
	const [payStep, setPayStep] = useState("idle");

	// Re-fetch a FRESH 402 at click time. Requirements parsed from an earlier
	// 402 rot in page state: the signed authorization's validity window is
	// computed from them at signing time, so a tab left open across a server
	// config change signs yesterday's window — the exact
	// authorization_validity_too_short failure Dan hit in two browsers whose
	// tabs predated the 7-day-window deploy. Returns null when the submit
	// succeeded outright (paywall off), else the freshly parsed requirements.
	const refreshPaymentRequirements = async () => {
		try {
			const { data, receipt: settledReceipt } = await submitStart();
			resetPaymentStepState();
			setNoSettlementNotice(!settledReceipt);
			if (GENERATION_QUOTE_ENABLED) fetchQuote();
			fetchCredits();
			// Same contract as startJob / finishStart: an accepted job takes
			// the user into the stream. Returning null without this used to
			// leave the armed pay button beside a quiet table-row update —
			// the exact "reads as a failed start" shape enterStartedJob exists
			// to close.
			enterStartedJob(data);
			return null;
		} catch (e) {
			const fresh = derivePaymentRequirements(extractPaymentRequiredHeader(e.headers));
			if (!fresh?.requirements) throw e;
			setPaymentRequirements(fresh);
			setPaymentRequirementsAt(Date.now());
			setPaywallQuoteRaw(e?.detail?.quote ?? null);
			return fresh;
		}
	};

	// True when the browser refused to OPEN the signing prompt because the
	// tap's transient user activation was already consumed by earlier awaits
	// (ox Authentication.SignFailedError, surfaced verbatim as "Failed to
	// request credential." in Dan's 2026-08-21 field test — the deposit
	// prompt, close to the tap, succeeded; the payment prompt after the
	// deposit's confirmation wait was refused every time, iPad and Mac
	// alike). Not a wallet failure: the fix is a fresh tap that reaches the
	// ceremony first.
	const isActivationRefusal = (e) =>
		e?.name === "Authentication.SignFailedError" ||
		/failed to request credential/i.test(e?.message || "") ||
		/failed to request credential/i.test(e?.cause?.message || "");

	// Requirements already held in page state are signable without a
	// click-time refetch while younger than this — the refetch was #1459's
	// staleness guard, but awaiting it before the signature is exactly what
	// kills the WebAuthn ceremony on WebKit. 10 minutes is far inside the
	// 7-day validity window (#1452) while still bounding how long a server
	// config change can sit unnoticed under an idle tab.
	const REQUIREMENTS_FRESH_MS = 10 * 60 * 1000;
	const heldSignableRequirements = () => {
		const held = paymentRequirements;
		if (!held?.requirements) return null;
		if (Date.now() - paymentRequirementsAt > REQUIREMENTS_FRESH_MS) return null;
		return held;
	};

	const finishStart = async (header) => {
		setPayStep("starting");
		const { data, receipt: settledReceipt } = await submitStart({ "Payment-Signature": header });
		resetPaymentStepState();
		setNoSettlementNotice(!settledReceipt);
		if (GENERATION_QUOTE_ENABLED) fetchQuote();
		fetchCredits();
		enterStartedJob(data);
	};

	// ── One tap when funded: the signing ceremony runs FIRST, inside the
	// tap's activation, against held requirements and the held balance.
	// Deposit-needed taps run refetch → deposit (its prompt is close enough
	// to the tap to survive), then TRY to finish; a browser that refuses
	// the second ceremony arms payStep "confirm" so the next tap gives the
	// payment signature its own activation. ──
	const handlePayAndGenerate = async () => {
		setPaying(true);
		setPaymentMessage("");
		setDepositError("");
		try {
			// ── Passkey path: the device payment key does everything. ──
			// No WebAuthn ceremony is involved in SIGNING (local ECDSA key),
			// so there is no user-activation ordering constraint here — the
			// only prompt is the funding user-op, and only when the key's
			// balance is short. See ../payment-session.js for why this rail
			// exists (Circle's facilitator verifies EOA signatures only).
			if (paymentWalletKind() === "circle") {
				setPayStep("refreshing");
				const fresh = heldSignableRequirements() ?? (await refreshPaymentRequirements());
				if (!fresh) return; // started without payment (paywall off)
				const req = fresh.requirements;
				const need = BigInt(req.amount);
				const session = getOrCreateSessionAccount(walletAddr);
				await ensureSessionLinked(session);
				let bal = await getGatewayBalance(req, session.address);
				if (bal == null || bal < need) {
					const amountRaw = parseUsdcAmount(depositAmount || "20.00");
					if (amountRaw < need) {
						throw new Error("Deposit amount must at least cover the generation price.");
					}
					await depositToGateway(req, amountRaw, (step) =>
						setPayStep(step === "approving" ? "approving" : "depositing"),
					);
					bal = await getGatewayBalance(req, session.address);
					setGatewayBalance(bal);
				}
				setPayStep("signing");
				const header = await signGatewayPayment({
					requirements: req,
					resource: fresh.resource,
					x402Version: fresh.x402Version,
					payerAddress: session.address,
				});
				setPayStep("starting");
				const { data, receipt: settledReceipt } = await submitStart({
					"Payment-Signature": header,
					// The paywall binds authorization.from to a LINKED wallet
					// resolved from this header — it must name the payment
					// key, not the connected passkey account.
					"X-Wallet-Address": session.address,
				});
				resetPaymentStepState();
				setNoSettlementNotice(!settledReceipt);
				if (GENERATION_QUOTE_ENABLED) fetchQuote();
				fetchCredits();
				enterStartedJob(data);
				return;
			}

			const held = heldSignableRequirements();
			const funded =
				held != null &&
				requiredAmountRaw != null &&
				gatewayBalance != null &&
				gatewayBalance >= requiredAmountRaw;
			if (funded) {
				// NOTHING may be awaited between the tap and this call.
				setPayStep("signing");
				const header = await signGatewayPayment({
					requirements: held.requirements,
					resource: held.resource,
					x402Version: held.x402Version,
					payerAddress: walletAddr,
				});
				await finishStart(header);
				return;
			}
			setPayStep("refreshing");
			const fresh = held ?? (await refreshPaymentRequirements());
			if (!fresh) return; // started without payment (paywall off)
			const req = fresh.requirements;
			const need = BigInt(req.amount);
			let bal = await getGatewayBalance(req, walletAddr);
			if (bal == null || bal < need) {
				const amountRaw = parseUsdcAmount(depositAmount || "20.00");
				if (amountRaw < need) {
					throw new Error("Deposit amount must at least cover the generation price.");
				}
				await depositToGateway(req, amountRaw, (step) =>
					setPayStep(step === "approving" ? "approving" : "depositing"),
				);
				bal = await getGatewayBalance(req, walletAddr);
				setGatewayBalance(bal);
			}
			setPayStep("signing");
			let header;
			try {
				header = await signGatewayPayment({
					requirements: req,
					resource: fresh.resource,
					x402Version: fresh.x402Version,
					payerAddress: walletAddr,
				});
			} catch (e) {
				if (isActivationRefusal(e)) {
					// Deposit landed; the browser just needs a fresh tap for
					// the payment prompt. Not an error state.
					setPayStep("confirm");
					return;
				}
				throw e;
			}
			await finishStart(header);
		} catch (e) {
			if (isActivationRefusal(e)) {
				setPayStep("confirm");
			} else {
				const gate = deriveWalletGateView(e);
				if (gate) {
					setPaymentStatus(PAYMENT_STATUS.WALLET_LINK_REQUIRED);
					setWalletGateView(gate);
				} else {
					setPaymentMessage(
						paymentErrorMessage(e, e?.shortMessage || e?.message || "Payment failed — try again."),
					);
				}
			}
		} finally {
			setPaying(false);
			setPayStep((s) => (s === "confirm" ? s : "idle"));
		}
	};



	// ── Apply an example brief ──
	const applyExample = (ex) => {
		setIntent(ex.brief);
		if (Array.isArray(ex.suggestedAssets) && ex.suggestedAssets.length) {
			setSelectedAssets(
				ex.suggestedAssets.filter((a) => SUPPORTED_ASSETS.includes(a)),
			);
		}
	};

	// Surprise Me: draw a brief the user has not just seen and drop it in the
	// box. `lastSurpriseId` is the exclusion, so pressing twice always moves.
	const handleSurprise = () => {
		const pick = pickSurpriseBrief(SURPRISE_BRIEFS, lastSurpriseId);
		if (!pick) return;
		setLastSurpriseId(pick.id);
		setSurpriseLabel(pick.label);
		applyExample(pick);
	};

	// ── Asset picker helpers ──
	const toggleAsset = (a) =>
		setSelectedAssets((prev) =>
			prev.includes(a) ? prev.filter((x) => x !== a) : [...prev, a],
		);
	const clearAssets = () => setSelectedAssets([]);

	const filteredAssetGroups = useMemo(() => {
		const q = assetQuery.trim().toLowerCase();
		if (!q) return ASSET_GROUPS;
		return ASSET_GROUPS.map((g) => ({
			...g,
			assets: g.assets.filter((a) => a.toLowerCase().includes(q)),
		})).filter((g) => g.assets.length > 0);
	}, [assetQuery]);

	// ── Drill-down: open stream for a job ──
	const handleDrillIn = (jobId) => setDrillInJobId(jobId);
	const handleBackToTable = () => setDrillInJobId(null);

	// ── Ways forward from a run that found too few papers ──
	// Both leave the stream view and put the user back in the brief box: the
	// failure card's whole point is that there IS a next move.
	const handleBroaden = (term, steer) => {
		setDrillInJobId(null);
		setIntent((prev) => {
			const base = (prev || steer || "").trim();
			if (!base) return term;
			// Never duplicate a term the brief already contains.
			if (base.toLowerCase().includes(term.toLowerCase())) return base;
			return `${base.replace(/[\s.]+$/, "")}, including ${term}`;
		});
		setSurpriseLabel("");
		setFocusBriefTick((n) => n + 1);
	};

	const handleSurpriseFromStream = () => {
		setDrillInJobId(null);
		handleSurprise();
		setFocusBriefTick((n) => n + 1);
	};

	useEffect(() => {
		if (focusBriefTick > 0) briefRef.current?.focus();
	}, [focusBriefTick]);

	// ─── Drill-down view (stream for a selected job) ───────────
	if (drillInJobId) {
		return (
			<div className="generate-page generate-page--stream">
				<div className="app-page-heading generate-page__heading">
					<button
						type="button"
						className="btn btn-outline btn-sm"
						onClick={handleBackToTable}
						style={{ marginBottom: 12 }}
					>
						← Back to generations
					</button>
					<h2 className="serif text-[1.6rem] mb-1">Generation stream</h2>
					<p className="caption" style={{ color: "var(--text-3)" }}>
						Job {drillInJobId.slice(0, 10)}… — full history replayed from the
						start. Navigate away and come back; the job continues running in the
						background.
					</p>
				</div>
				<GenerationStream
					jobId={drillInJobId}
					onDone={() => {}}
					onReset={handleBackToTable}
					onPipelineSelected={() => {}}
					onNavigate={onNavigate}
					onBroaden={handleBroaden}
					onSurprise={handleSurpriseFromStream}
					hideReset
				/>
			</div>
		);
	}

	// ─── Main view (model → brief → job table) ─────────────────
	return (
		<div className="generate-page">
			<header className="app-page-heading generate-page__heading">
				<p className="app-eyebrow">Strategy synthesis</p>
				<h1>Generate a strategy</h1>
				<p>
					Describe the outcome you want. Archimedes retrieves relevant q-fin
					papers, debates candidate methods, sizes positions, and sends one
					winner to the rigor gate.
				</p>
			</header>

			{/* Free-generation allowance (#1643) — one isolated component, one
			    mount point. Renders nothing when there is no honest number to
			    show (signed out, request failed, backend reported null). */}

			<div className="generate-workbench">
				{/* ── 1. BRIEF INPUT + SUBMIT ── */}
				<section className="card generate-brief">
					{/* Strategy name — promoted from Advanced options so it's seen before the brief */}
					{/* Real <label htmlFor> associations. These three fields — the
					    strategy name, the brief, and the asset search below — were
					    labelled by plain <div>s and placeholders only, so a screen
					    reader announced the product's single most important control
					    as an anonymous "edit, multiline" the moment the user typed
					    the first character (3.3.2 / 4.1.2). */}
					<div className="mb-3">
						<label className="label mb-1 block" htmlFor="generate-strategy-name">
							Strategy name (optional)
						</label>
						<input
							id="generate-strategy-name"
							type="text"
							value={strategyName}
							onChange={(e) => setStrategyName(e.target.value)}
							placeholder="Leave blank — backend auto-derives a name"
							maxLength={80}
							className="chat-input w-full px-2.5 py-1.5"
							disabled={starting}
						/>
						<p className="caption mt-1" style={{ color: "var(--text-3)" }}>
							Short and memorable — leave blank to auto-name.{" "}
							{strategyName.length}/80
						</p>
					</div>

					<label className="label mb-1 block" htmlFor="generate-brief">
						Your brief
					</label>
					{/* Short hint only. Tutorial prose lives in
					    docs/writing-a-brief.md (#1642); the limits and what must
					    NOT be in a brief in docs/brief-guidelines.md (#1801). */}
					<p
						className="caption mb-2"
						id="generate-brief-help"
						style={{ color: "var(--text-3)" }}
					>
						Name assets, a mechanism, and a goal.{" "}
						<a
							className="generate-brief-guide-link"
							href="https://github.com/aprin-labs/archimedes/blob/main/docs/writing-a-brief.md"
							target="_blank"
							rel="noreferrer"
						>
							How to write a brief →
						</a>{" "}
						<a
							className="generate-brief-guide-link"
							href="https://github.com/aprin-labs/archimedes/blob/main/docs/brief-guidelines.md"
							target="_blank"
							rel="noreferrer"
						>
							What a brief may not contain →
						</a>
					</p>

					{/* The browser cap is a courtesy (it stops a paste becoming
					    an unexplained 422), NOT the guard — the request schema
					    and services.brief_screen enforce it server-side. */}
					<textarea
						id="generate-brief"
						aria-describedby="generate-brief-help generate-brief-count"
						ref={briefRef}
						value={intent}
						onChange={(e) => setIntent(e.target.value)}
						placeholder="e.g. blend momentum, quality and a gold hedge across major ETFs with volatility-managed sizing for idle USDC"
						rows={3}
						maxLength={BRIEF_MAX_LEN}
						className="chat-input w-full p-2.5 leading-relaxed"
						disabled={starting}
					/>
					<p
						className="caption mt-1 mb-3"
						id="generate-brief-count"
						style={{
							color:
								intent.length >= BRIEF_MAX_LEN
									? "var(--warn, var(--text-2))"
									: "var(--text-3)",
						}}
					>
						{intent.length}/{BRIEF_MAX_LEN}
						{intent.length >= BRIEF_MAX_LEN
							? " — at the limit; a brief works best at one or two sentences."
							: ""}
					</p>

					{/* Surprise Me (#1642) — the ONLY example-related control on the
					    page. No brief text renders here in any state: the pick goes
					    into the textarea above, and this region announces only which
					    one landed (a label, for the screen-reader user who cannot see
					    the box repaint). Nothing renders before the first press. */}
					<div className="generate-surprise mb-3">
						<button
							type="button"
							onClick={handleSurprise}
							disabled={starting}
							className="generate-surprise-btn"
						>
							<span
								className="i-lucide-shuffle w-4 h-4"
								aria-hidden="true"
							/>
							Surprise me
						</button>
						<p
							className="caption generate-surprise-hint mb-0"
							role="status"
							aria-live="polite"
						>
							{surpriseLabel
								? `Filled in: ${surpriseLabel}. Press again for another.`
								: "Fills the box with an example brief — a different one each press."}
						</p>
					</div>

					{/* Advanced options — collapsed by default */}
					<div
						className="mb-3"
						style={{
							borderTop: "1px solid var(--glass-border)",
							paddingTop: 10,
						}}
					>
						<button
							type="button"
							className="generate-advanced-toggle"
							onClick={() => setAdvancedOpen((o) => !o)}
							aria-expanded={advancedOpen}
						>
							<span
								className={`${advancedOpen ? "i-lucide-chevron-down" : "i-lucide-chevron-right"} w-3.5 h-3.5`}
							/>
							Advanced options
							{(selectedAssets.length > 0 ||
								riskAppetite !== "moderate" ||
								depth !== DEFAULT_DEPTH) && (
								<span
									className="tag tag-accent"
									style={{ fontSize: "0.7rem", padding: "1px 6px" }}
								>
									active
								</span>
							)}
						</button>

						{advancedOpen && (
							<div style={{ marginTop: 12 }}>
								{/* Risk + Depth selects */}
								<div className="flex gap-4 flex-wrap mb-3">
									<label className="caption flex items-center gap-1.5">
										Risk
										<select
											value={riskAppetite}
											onChange={(e) => setRiskAppetite(e.target.value)}
											className="chat-input w-auto px-2 py-1"
											disabled={starting}
										>
											{RISK_PROFILES.map((r) => (
												<option key={r.id} value={r.id}>
													{r.label}
												</option>
											))}
										</select>
									</label>
									<label className="caption flex items-center gap-1.5">
										Depth
										<select
											value={depth}
											onChange={(e) => setDepth(Number(e.target.value))}
											className="chat-input w-auto px-2 py-1"
											disabled={starting}
											title="How many papers the engine retrieves and shows the model. It is asked to fuse at least 5 of them, and to name what it rejected when it cites fewer."
										>
											{DEPTH_OPTIONS.map((n) => (
												<option key={n} value={n}>
													{n}
												</option>
											))}
										</select>
									</label>
								</div>

								{/* Asset picker */}
								<div>
									<div className="flex items-center justify-between mb-1">
										<div className="label" style={{ fontSize: "0.82rem" }}>
											Assets (optional)
											{selectedAssets.length > 0
												? ` · ${selectedAssets.length} selected`
												: ""}
										</div>
										{selectedAssets.length > 0 && (
											<button
												type="button"
												onClick={clearAssets}
												className="caption"
												style={{
													background: "none",
													border: "none",
													cursor: "pointer",
													color: "var(--accent)",
												}}
											>
												Clear
											</button>
										)}
									</div>
									<p
										className="caption mb-2"
										style={{ color: "var(--text-3)" }}
									>
										Steer the universe. Leave empty to use the full supported
										set — {SUPPORTED_ASSETS.length} assets. Engine C caps each
										spec at {ENGINE_C_ASSET_CAP} and grades them as independent
										sleeves.
									</p>
									<input
										type="text"
										value={assetQuery}
										onChange={(e) => setAssetQuery(e.target.value)}
										placeholder="Search assets (e.g. SPY, GOLD, BTC)…"
										aria-label="Search assets"
										className="chat-input w-full mb-2 px-2.5 py-1.5"
										disabled={starting}
									/>
									<div
										style={{
											maxHeight: 180,
											overflowY: "auto",
											paddingRight: 4,
										}}
									>
										{filteredAssetGroups.length === 0 && (
											<div
												className="caption"
												style={{ color: "var(--text-3)" }}
											>
												No assets match "{assetQuery}".
											</div>
										)}
										{/* Each ticker is a real toggle button: as a click-only
										    <span> the universe picker had no tab stop and no
										    role, so a keyboard user could clear the selection
										    (the "Clear" affordance is a real button) but never
										    set one (2.1.1 / 4.1.2). */}
										{filteredAssetGroups.map((group) => (
											<div key={group.id} className="mb-2">
												<div
													className="caption mb-1"
													style={{ color: "var(--text-3)" }}
												>
													{group.label}
												</div>
												<div
													className="flex gap-1.5 flex-wrap"
													role="group"
													aria-label={`${group.label} assets`}
												>
													{group.assets.map((a) => (
														<button
															key={a}
															type="button"
															className={`tag ${selectedAssets.includes(a) ? "tag-accent" : "tag-muted"} cursor-pointer`}
															aria-pressed={selectedAssets.includes(a)}
															onClick={() => toggleAsset(a)}
														>
															{a}
														</button>
													))}
												</div>
											</div>
										))}
									</div>
								</div>
							</div>
						)}
					</div>

					{/* ── Upfront cost quote (feature-flagged, ratified #1296 shape) ── */}
					{GENERATION_QUOTE_ENABLED && (
						<div className="generate-quote mb-3">
							{quoteStatus === "loading" && (
								<p className="caption" style={{ color: "var(--text-3)" }}>
									Fetching cost quote…
								</p>
							)}
							{quoteStatus === "error" && (
								<div className="info-box warning">
									Cost quote unavailable ({quoteError}).{" "}
									<button
										type="button"
										onClick={fetchQuote}
										className="caption"
										style={{
											background: "none",
											border: "none",
											cursor: "pointer",
											color: "var(--accent)",
											textDecoration: "underline",
											padding: 0,
										}}
									>
										Try again
									</button>
								</div>
							)}
							{quoteStatus === "ready" && quoteView && (
								<div className="info-box">
									<div className="flex items-center flex-wrap gap-2">
										<span className="label">Cost to generate</span>
										<span className="tag tag-accent">
											{quoteView.price} {quoteView.asset}
										</span>
										{quoteView.paymentRequired ? (
											<span className="tag tag-info">testnet USDC</span>
										) : (
											<span className="tag tag-muted">payment not required yet</span>
										)}
										{quoteView.paymentRequired && quoteView.dryRun && (
											<span className="tag tag-warning">
												test mode — payment accepted unverified
											</span>
										)}
									</div>
									<p
										className="caption mb-0"
										style={{ marginTop: 8, color: "var(--text-3)" }}
									>
										<span className="tag tag-positive" style={{ marginRight: 6 }}>
											free
										</span>
										Paper trading after generation costs nothing — this quote
										covers generation only.
									</p>
									{quoteView.paymentRequired && payerMismatch && (
										<p
											className="caption mb-0"
											style={{ marginTop: 6, color: "var(--text-3)" }}
										>
											Payments must be signed by your linked wallet (
											{shortAddr(payerMismatch.linked)}) — your connected wallet
											({shortAddr(payerMismatch.active)}) isn't linked yet.
										</p>
									)}
								</div>
							)}
						</div>
					)}

					{/* ── Paid generation credit notice (v8 Lane 1.3a) ──
					    _paywall_with_credit already spends an unspent credit
					    silently, BEFORE the paywall even runs — this just makes
					    that real behavior visible instead of leaving the payer
					    to discover it only from the receipt list.

					    Gated on quote.payment_required as well as the credit:
					    "no new charge" only says something true when there is a
					    charge to avoid. With the paywall flag off there is no
					    charge either way, and the sentence would imply the payer
					    is being spared one — so the notice stays out of the way
					    entirely rather than being softened into vagueness. */}
					{unspentCredit && quote?.payment_required && !creditNoticeDismissed && (
						<div
							className="info-box mb-3 flex items-center justify-between gap-2"
							role="status"
							aria-live="polite"
						>
							<span>
								You have a paid generation credit — this run will use
								it, no new charge.
							</span>
							<button
								type="button"
								onClick={() => setCreditNoticeDismissed(true)}
								className="caption"
								aria-label="Dismiss credit notice"
								style={{
									background: "none",
									border: "none",
									cursor: "pointer",
									color: "var(--text-3)",
									padding: 0,
									flexShrink: 0,
								}}
							>
								Dismiss
							</button>
						</div>
					)}

					{/* Submit row */}
					{/* ↑ That marker is load-bearing: ui/test/generation-credits.test.js
					    slices the credit notice's JSX from its own comment to this
					    exact string. Keep it verbatim, and keep new markup BELOW it
					    so the slice stays scoped to the notice.

					    ── Gating-banner mount point (#1643) ──
					    Deliberately empty here. The generation gating banner is an
					    isolated component owned by #1643; it mounts into this slot,
					    immediately above the submit controls, so the two changes do
					    not have to touch the same lines. #1642 builds no gating
					    logic — if this div is still empty, #1643 has not landed. */}
					<div className="generate-gate-slot" data-generate-gate-slot>
						<FreeGenerationBanner email={user?.email} />
					</div>

					{/* `generate-submit-row` (not a bare flex utility chain) because
					    the phone layout stacks it: the live status region above, a
					    full-width submit below — see the #1642 block in App.css. */}
					<div
						className="generate-submit-row flex items-center justify-between flex-wrap gap-2"
						style={{ marginTop: 2 }}
					>
						{/* Every outcome of pressing Generate — start failure, quote not
						    ready, wallet-link required, test mode, payments unavailable,
						    and the success receipt — paints into this one slot. None of
						    it was announced: the user heard the button label revert from
						    "Starting…" and was never told whether the job started or why
						    it failed (4.1.3). The live container is mounted
						    unconditionally, because a live region added at the same
						    moment as its text is usually missed by assistive tech. */}
						<div
							className="generate-submit-status"
							role="status"
							aria-live="polite"
							aria-atomic="true"
							style={{ flex: 1, display: "flex" }}
						>
						{paymentStatus === PAYMENT_STATUS.WALLET_LINK_REQUIRED ? (
							<div className="info-box warning" style={{ flex: 1 }}>
								<strong>{walletGateView?.title ?? "Wallet link required"}.</strong>{" "}
								{walletGateView?.message}
								{payerMismatch && (
									<p
										className="caption mb-0"
										style={{ marginTop: 6, color: "var(--text-3)" }}
									>
										Your connected wallet ({shortAddr(payerMismatch.active)})
										isn't linked to your account — your linked wallet is{" "}
										{shortAddr(payerMismatch.linked)}. Switch your wallet's
										active account to that one, or link the connected one below.
									</p>
								)}
								{walletGateView?.offerResend && (
									<ResendVerificationControl email={user?.email} />
								)}
								{" "}
								<button
									type="button"
									onClick={() =>
										window.dispatchEvent(new Event("open-wallet-modal"))
									}
									className="caption"
									style={{
										background: "none",
										border: "none",
										cursor: "pointer",
										color: "var(--accent)",
										textDecoration: "underline",
										padding: 0,
									}}
								>
									Connect / link wallet →
								</button>
							</div>
						) : paymentStatus === PAYMENT_STATUS.DRY_RUN || paymentStatus === PAYMENT_STATUS.LIVE_UNAVAILABLE ? (
							<div className="info-box warning" style={{ flex: 1 }}>
								<strong>Payment required.</strong>{" "}
								{effectiveQuoteView && (
									<>
										Generating costs{" "}
										<strong>
											{effectiveQuoteView.price} {effectiveQuoteView.asset}
										</strong>{" "}
										on {effectiveQuoteView.chain}
										{effectiveQuoteView.recipient ? <> to {shortAddr(effectiveQuoteView.recipient)}</> : null}
										{walletAddr ? <> — paid from your connected wallet ({shortAddr(walletAddr)})</> : null}.{" "}
									</>
								)}
								{effectiveQuoteView?.dryRun && (
									<span className="tag tag-warning" style={{ marginRight: 6 }}>
										test mode — server accepts payments unverified
									</span>
								)}
								{paymentRequirements?.error ? (
									<p className="caption mb-0" style={{ marginTop: 6 }}>
										{paymentRequirements.error === "no_gateway_option"
											? "This backend didn't offer a supported payment method (Circle Gateway) for this request — contact the team."
											: "Could not read the payment requirements from the server response. Try again."}
									</p>
								) : !walletAddr ? (
									<p className="caption mb-0" style={{ marginTop: 6 }}>
										Connect your wallet to pay.{" "}
										<button
											type="button"
											onClick={() => window.dispatchEvent(new Event("open-wallet-modal"))}
											className="caption"
											style={{
												background: "none",
												border: "none",
												cursor: "pointer",
												color: "var(--accent)",
												textDecoration: "underline",
												padding: 0,
											}}
										>
											Connect wallet →
										</button>
									</p>
								) : payerMismatch ? (
									<p className="caption mb-0" style={{ marginTop: 6 }}>
										Payments must be signed by your linked wallet (
										{shortAddr(payerMismatch.linked)}) — switch your connected wallet (
										{shortAddr(payerMismatch.active)}) to that one, or link the connected
										one below.
									</p>
								) : !walletSupportsPayment() ? (
									<div style={{ marginTop: 6 }}>
										<p className="caption mb-1">
											Connect the wallet you linked to pay — MetaMask, Coinbase, Brave,
											Phantom, or your Circle passkey wallet.
										</p>
										<button
											type="button"
											className="btn btn-outline btn-sm"
											onClick={() => window.dispatchEvent(new Event("open-wallet-modal"))}
										>
											Connect wallet →
										</button>
									</div>
								) : (
									<div style={{ marginTop: 6 }}>
										<p className="caption mb-1">
											{paymentWalletKind() === "circle" ? (
												// Passkey wallets pay through a device payment key
												// (see ../payment-session.js): Circle's nanopayments
												// rail verifies EOA signatures only, so the passkey
												// account funds a local key once and that key signs
												// each payment silently. Copy states the custody
												// bound plainly — the key lives in this browser and
												// only ever controls its own deposited balance.
												<>
													Payments run from a device payment key your passkey
													wallet funds — one passkey approval to set it up,
													then each generation settles with no prompts. The
													key stays in this browser and only controls its own
													deposited balance.
													{gatewayBalance != null && (
														<> Payment balance: ${(Number(gatewayBalance) / 1e6).toFixed(2)}.</>
													)}
												</>
											) : (
												<>
													Payments run from your Circle Gateway balance — a one-time
													deposit covers many generations; each one is then a single
													wallet signature.
													{gatewayBalance != null && (
														<> Your balance: ${(Number(gatewayBalance) / 1e6).toFixed(2)}.</>
													)}
												</>
											)}
										</p>
										<button
											type="button"
											className="btn btn-primary btn-sm"
											onClick={handlePayAndGenerate}
											disabled={paying}
										>
											{payStep === "refreshing"
												? "Checking price…"
												: payStep === "approving"
													? "Approve USDC in your wallet…"
													: payStep === "depositing"
														? "Depositing into Gateway…"
														: payStep === "signing"
															? "Sign the payment in your wallet…"
															: payStep === "starting"
																? "Starting generation…"
																: payStep === "confirm"
																	? `Tap to approve the ${fmtUsd(effectiveQuoteView?.price) ?? ""} payment →`
																	: `Pay ${fmtUsd(effectiveQuoteView?.price) ?? ""} & generate →`}
										</button>
										{(gatewayBalance == null ||
											(requiredAmountRaw != null && gatewayBalance < requiredAmountRaw)) && (
											<details style={{ marginTop: 6 }}>
												<summary className="caption" style={{ cursor: "pointer" }}>
													First payment? A one-time deposit of {depositAmount || "20.00"}{" "}
													USDC is included automatically (covers ~10 generations — edit)
												</summary>
												<input
													type="text"
													value={depositAmount}
													onChange={(e) => setDepositAmount(e.target.value)}
													aria-label="Deposit amount (USDC)"
													className="chat-input"
													style={{ width: 110, marginTop: 6 }}
													disabled={paying}
												/>
											</details>
										)}
										{depositError && (
											<p
												className="caption"
												style={{ color: "var(--negative, #ef4444)", marginTop: 4 }}
											>
												{depositError}
											</p>
										)}
									</div>
								)}
								{paymentMessage && (
									<p className="caption mb-0" style={{ marginTop: 6 }}>
										{paymentMessage}
									</p>
								)}
							</div>
						) : startError ? (
							<div className="info-box warning" style={{ flex: 1 }}>
								{startError}
							</div>
						) : noSettlementNotice ? (
							<div className="info-box" style={{ flex: 1 }}>
								<span className="tag tag-warning" style={{ marginRight: 6 }}>
									test mode
								</span>
								Accepted without settlement (server test mode) — no funds moved.
							</div>
						) : receipt ? (
							<div className="info-box" style={{ flex: 1 }}>
								<span className="tag tag-positive" style={{ marginRight: 6 }}>
									paid
								</span>
								Settlement receipt: <code>{receipt}</code>
							</div>
						) : (
							<div />
						)}
						</div>
						{!payPanelReady && (
							<button
								className="btn btn-primary generate-submit-btn"
								onClick={startJob}
								disabled={starting || !intent.trim() || !quoteReady}
								aria-describedby={
									!intent.trim() ? "generate-submit-hint" : undefined
								}
							>
								{starting
									? "Starting…"
									: GENERATION_QUOTE_ENABLED
										? "Approve & Generate →"
										: "Generate →"}
							</button>
						)}
					</div>
					{/* The submit button is disabled until a brief exists; state the
					    cause rather than leaving the form looking stuck (3.3.1). */}
					{!intent.trim() && (
						<p
							id="generate-submit-hint"
							className="caption mt-2 mb-0"
							style={{ color: "var(--text-3)" }}
						>
							Enter a brief above to enable generation.
						</p>
					)}
				</section>

				<aside
					className="generate-context-rail"
					aria-label="Generation context"
				>
					<ModelCostPanel
						selectedModel={selectedModel}
						onSelectModel={setSelectedModel}
					/>
					<div className="generate-context-note">
						<p className="label">Pipeline inputs</p>
						<dl>
							<div>
								<dt>Your brief</dt>
								<dd>goal, risk, assets</dd>
							</div>
							<div>
								<dt>Market context</dt>
								<dd>current regime signals</dd>
							</div>
							<div>
								<dt>Research</dt>
								<dd>paper methods and evidence</dd>
							</div>
						</dl>
						<p>
							Output: one candidate, considered rejects, and a visible rigor
							verdict.
						</p>
					</div>
				</aside>
			</div>

			{/* ── 2. JOB REGISTER ── */}
			<section className="generate-register" aria-label="Generation register">
				{/* `onNavigate` was already in this component's scope (line ~92) and
				    threaded into GenerationStream, but never into the job register —
				    so a finished generation could only reopen its own stream, never
				    the strategy it produced (#1646). */}
				<GenerationStatus
					activeJobId={lastJobId}
					onDrillIn={handleDrillIn}
					onNavigate={onNavigate}
				/>
			</section>
		</div>
	);
}
