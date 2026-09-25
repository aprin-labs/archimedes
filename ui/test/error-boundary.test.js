import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Same shape as a11y.test.js / routes.test.js: readFileSync + regex pins on
// the source, no DOM — node --test has no JSX loader, so ErrorBoundary.jsx,
// main.jsx and AuthenticatedApp.jsx (all .jsx) can't be imported directly.
//
// Every assertion here was confirmed to FAIL against the pre-fix tree (no
// ErrorBoundary.jsx existed; main.jsx mounted <App /> bare; AuthenticatedApp
// rendered {renderPage()} unwrapped) — see the PR body for the transcript
// (CLAUDE.md § "A guard must be shown to reject something").

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");

const boundary = src("components/ErrorBoundary.jsx");
const main = src("main.jsx");
const authenticatedApp = src("AuthenticatedApp.jsx");

test("ErrorBoundary.jsx is a real class-component error boundary", () => {
	assert.match(boundary, /class ErrorBoundary extends Component/);
	assert.match(boundary, /static getDerivedStateFromError\s*\(/);
	assert.match(boundary, /componentDidCatch\s*\(/);
	// componentDidCatch must actually report, not swallow silently. Scoped to
	// the method body (stops at the first `}`, not `[\s\S]*?` which would
	// happily match a console.error() anywhere later in the file even if
	// this method's own body were emptied out) and requires the caught
	// error itself — not just the log prefix — be one of the args, via a
	// backreference to whatever componentDidCatch's first parameter is
	// actually named.
	assert.match(
		boundary,
		/componentDidCatch\s*\(\s*(\w+)[^)]*\)\s*\{[^}]*console\.error\([^)]*\b\1\b[^)]*\)/,
	);
});

test("the fallback names the failure and offers a real reload control, not a blank card", () => {
	assert.match(boundary, /role="alert"/);
	assert.match(boundary, /<button[^>]*onClick=\{?this\.handleReload\}?[^>]*>/);
	// "names the failure": the actual error message/detail is interpolated
	// into the rendered copy, not a fixed generic string only. `{detail}`
	// alone only pins the identifier name in the JSX — it's equally happy
	// with `const detail = "Something went wrong."`. Requiring the `detail`
	// assignment itself to derive from `error` closes that gap without
	// pinning the exact `instanceof Error ? ... : String(error)` shape.
	assert.match(boundary, /\{detail\}/);
	assert.match(boundary, /const\s+detail\s*=\s*[^;]*error/);
	assert.match(boundary, /window\.location\.reload\(\)/);
});

test("main.jsx wraps <App in <ErrorBoundary", () => {
	assert.match(main, /import ErrorBoundary from ['"]\.\/components\/ErrorBoundary(\.jsx)?['"]/);
	assert.match(main, /<ErrorBoundary>[\s\S]*?<App\s*\/>[\s\S]*?<\/ErrorBoundary>/);
});

test("AuthenticatedApp.jsx wraps {renderPage()} in an <ErrorBoundary keyed on the page and every parameterised sub-route", () => {
	// key={route.page} alone doesn't change identity between two instances
	// of the same parameterised page (e.g. /strategy/1 -> /strategy/2 are
	// both route.page === "strategy"), so a crashed detail page would stay
	// crashed after navigating to a sibling. The key folds in
	// strategyId/vaultAddress/traceId/highlight/tab so React remounts (and
	// clears the boundary) on ALL of those sub-route changes, not just a
	// page change and not just strategy/vault-detail/market-strategy —
	// reasoning's traceId and library's highlight/tab must remount too.
	assert.match(
		authenticatedApp,
		/import ErrorBoundary from ["']\.\/components\/ErrorBoundary["']/,
	);
	assert.match(
		authenticatedApp,
		// The boundary must be keyed on the page + every parameterised sub-route
		// and enclose renderPage(). A Suspense wrapper between them is allowed
		// (lazy routes need a fallback INSIDE the boundary so a chunk-load
		// failure is caught by it) — what the guard rejects is a missing key
		// segment or renderPage() escaping the boundary.
		/<ErrorBoundary\s*key=\{`\$\{route\.page\}:\$\{route\.strategyId \?\? ""\}:\$\{route\.vaultAddress \?\? ""\}:\$\{route\.traceId \?\? ""\}:\$\{route\.highlight \?\? ""\}:\$\{route\.tab \?\? ""\}`\}\s*>\s*(?:<Suspense[\s\S]*?>\s*)?\{renderPage\(\)\}\s*(?:<\/Suspense>\s*)?<\/ErrorBoundary>/,
	);
});

test("the has-errored flag is a separate boolean, not the truthiness of the caught value", () => {
	// A boundary that uses `error` itself as the sentinel stays transparent
	// for a falsy throw (null/undefined/""/0): render() would return
	// this.props.children, which just threw, and React unmounts the root
	// anyway — the exact outage this component exists to prevent. Pin the
	// separate-boolean shape so it can't be silently reverted back to
	// `if (!error) return this.props.children`.
	assert.match(boundary, /hasError:\s*false/);
	assert.match(boundary, /static getDerivedStateFromError\s*\([^)]*\)\s*\{\s*return\s*\{\s*hasError:\s*true/);
	assert.match(boundary, /if\s*\(\s*!hasError\s*\)\s*return this\.props\.children/);
});

test("the two boundaries are distinct wrappers, not the same one reused", () => {
	// The per-page boundary is keyed (resets on navigation); the root
	// boundary in main.jsx is not. A single shared boundary would turn a
	// per-page crash into a full-page takeover (anti-goal).
	assert.doesNotMatch(main, /key=\{`\$\{route\.page\}/);
	assert.match(authenticatedApp, /key=\{`\$\{route\.page\}/);
});
