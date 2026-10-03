// The "Draft — under review" banner carried by /privacy and /terms.
//
// DO NOT REMOVE THIS COMPONENT OR ITS USES. These two pages were drafted by
// reading the code, not by a lawyer, and the owner has not yet reviewed them.
// The banner is the honest label on that state (CLAUDE.md § "Claims must be
// true"): a policy page presented as settled, when nobody has approved it, is
// a false claim about the document itself — and these are the pages a user or
// a Google OAuth reviewer reads to find out what we do with their data.
//
// Removing it is the OWNER'S call, made when he has actually read both pages.
// It is not a lint fix, not a polish pass, and not something an agent decides
// on its own. The same rule governs `Last updated` — it stays the literal
// string "[pending owner approval]" until he approves and dates them, because
// a date is a claim that someone signed off on that day.
export default function PolicyBanner() {
	return (
		<div className="policy-banner" role="note">
			<strong>Draft — under review.</strong> This page was written by reading
			what the software actually does, and is waiting on the owner&rsquo;s
			review. It has not been reviewed by a lawyer and is not legal advice.
			Treat it as an honest description of current behaviour rather than a
			finished legal document.
		</div>
	);
}
