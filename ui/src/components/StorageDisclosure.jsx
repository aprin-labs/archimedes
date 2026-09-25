import {
	CATEGORY_LABELS,
	CATEGORY_SUMMARIES,
	STORAGE_INVENTORY,
	displayName,
	entriesInCategory,
	lookupEntry,
} from "../storage-consent.js";
import ConsentChoices from "./ConsentChoices";

// The policy-page storage disclosure (#1647). Mounted by Security.jsx today;
// #1432's Privacy.jsx can mount the same component without copying a word of
// it, which is the point — the table below is RENDERED from
// STORAGE_INVENTORY, never transcribed from it, so it cannot drift from the
// keys the gate actually enforces.

// Every category, in the order a reader should meet them: what you cannot
// turn off (and why), then what you can, then the two footnote categories.
const SECTION_ORDER = [
	"necessary",
	"functional",
	"analytics",
	"consent",
	"legacy",
];

// The session cookies this page must always disclose. NOT a second copy of
// the table: these are ids looked up in the shared inventory below, and a
// missing one renders a loud failure instead of a quietly shorter page. The
// three are named because they are the ones a reader is most likely to be
// looking for — the two auth sessions and the analytics id — and losing one
// from the disclosure is the specific regression worth failing on.
export const REQUIRED_COOKIES = [
	"better-auth.session_token",
	"archimedes_session",
	"archimedes_vid",
];

function StorageTable({ category }) {
	const entries = entriesInCategory(category);
	if (entries.length === 0) return null;
	return (
		<div className="storage-disclosure__group">
			<h3 className="storage-disclosure__group-title">
				{CATEGORY_LABELS[category]}
				<span>
					{entries.length} {entries.length === 1 ? "key" : "keys"}
				</span>
			</h3>
			<p className="storage-disclosure__group-note">
				{CATEGORY_SUMMARIES[category]}
			</p>
			<div className="storage-disclosure__scroll">
				<table className="storage-disclosure__table">
					<caption className="storage-disclosure__caption">
						{CATEGORY_LABELS[category]} storage, as set by the current code.
					</caption>
					<thead>
						<tr>
							<th scope="col">Key</th>
							<th scope="col">Store</th>
							<th scope="col">Set in</th>
							<th scope="col">What it is for</th>
							<th scope="col">What it reveals</th>
							<th scope="col">If it is off</th>
						</tr>
					</thead>
					<tbody>
						{entries.map((entry) => (
							<tr key={entry.name}>
								<th scope="row">
									<code>{displayName(entry)}</code>
								</th>
								<td>{entry.store}</td>
								<td>
									<code>{entry.source}</code>
								</td>
								<td>{entry.purpose}</td>
								<td>{entry.reveals}</td>
								<td>{entry.onReject}</td>
							</tr>
						))}
					</tbody>
				</table>
			</div>
		</div>
	);
}

export default function StorageDisclosure() {
	const missingCookies = REQUIRED_COOKIES.filter((name) => {
		const entry = lookupEntry(name);
		return !entry || entry.store !== "cookie";
	});

	return (
		<section
			id="storage-disclosure"
			className="public-section storage-disclosure"
			aria-labelledby="storage-disclosure-title"
		>
			<div className="public-shell">
				<div className="security-section-heading">
					<p className="public-overline">Browser storage</p>
					<h2 id="storage-disclosure-title">
						Every cookie and key, and what each one reveals.
					</h2>
					<p>
						{STORAGE_INVENTORY.length} entries, taken from the code rather than
						from memory: the same list the consent gate reads before any write,
						and the same list a test re-derives from the source on every run. No
						third-party analytics, no advertising, no consent-management SDK —
						this page&apos;s content-security policy would not load one.
					</p>
				</div>

				{missingCookies.length > 0 ? (
					<p className="storage-disclosure__error" role="alert">
						Disclosure incomplete: {missingCookies.join(", ")} is missing from
						the storage inventory. Report this — the page is under-disclosing.
					</p>
				) : null}

				{SECTION_ORDER.map((category) => (
					<StorageTable category={category} key={category} />
				))}

				<ConsentChoices />
			</div>
		</section>
	);
}
