import { Component } from "react";

// React 19 unmounts the whole subtree under an uncaught render error and,
// with no boundary anywhere in the tree, that meant the entire root — a
// visitor sees an empty <div id="root"> with no message and no way back
// (#1357). This is a real class component: getDerivedStateFromError /
// componentDidCatch are the only React APIs that can intercept a render
// throw — there is no hook equivalent.
const LOG_PREFIX = "[ErrorBoundary]";

export default class ErrorBoundary extends Component {
	constructor(props) {
		super(props);
		this.state = { hasError: false, error: null };
	}

	// hasError is a separate boolean, not "error is truthy": a descendant
	// that throws null/undefined/""/0 is a rare but legal React throw, and
	// using the caught value itself as the sentinel would leave the
	// boundary transparent for exactly those cases — render() would return
	// this.props.children, which just threw, React re-throws in the same
	// boundary, gives up, and unmounts the root. That's the blank-page
	// outage this component exists to prevent (#1357).
	static getDerivedStateFromError(error) {
		return { hasError: true, error };
	}

	componentDidCatch(error, info) {
		// console.error is the only reporting channel in scope for this issue
		// (no Sentry et al) — see the issue's anti-goals.
		console.error(LOG_PREFIX, error, info?.componentStack);
	}

	handleReload = () => {
		window.location.reload();
	};

	render() {
		const { hasError, error } = this.state;
		if (!hasError) return this.props.children;

		// Name the actual failure rather than a generic "Something went
		// wrong" card with no way out — a blank-but-polite fallback is the
		// same outage with better manners.
		const detail = error instanceof Error ? error.message : String(error);
		// data-error-boundary="crashed" is the stable marker the deploy's
		// headless-Chrome render gate (.github/scripts/spa_render_check.mjs)
		// fails on: this card fills #root, so "#root has children" alone
		// would call a crashed app rendered.
		return (
			<div
				className="card error"
				role="alert"
				data-error-boundary="crashed"
				style={{ margin: 16 }}
			>
				<h2 style={{ marginTop: 0 }}>
					{this.props.label ? `${this.props.label} crashed` : "This page crashed"}
				</h2>
				<p style={{ color: "var(--text-2)" }}>{detail}</p>
				<button type="button" className="btn-primary" onClick={this.handleReload}>
					Reload
				</button>
			</div>
		);
	}
}
