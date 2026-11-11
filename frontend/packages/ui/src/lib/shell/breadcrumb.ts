/**
 * The shell's breadcrumb: every path segment of the route is a crumb, the first one the area
 * (`catalog`, `projects`, …), each linking back up its own path.
 */

export type Crumb = {
	id: string;
	label: string;
	/** The absolute, domain-relative path this crumb points at (`/data/tables`). */
	href: string;
};

/** Route words that are INITIALISMS, so sentence-casing does not turn them into words.
 *
 *  `/admin/dlq` sentence-cased to "Dlq", which reads as a misspelling rather than a name — the nav
 *  calls that page DLQ, and a crumb that renames the thing it points at is worse than an ugly one.
 *
 *  Deliberately a CLOSED list of the console's own route words, not a heuristic (a "≤4 letters ⇒
 *  acronym" rule would shout at `events`, `audit`); adding one is a decision, which is the point. */
const INITIALISMS = new Set(['dlq']);

/** Percent-decode a path segment for display, falling back to the raw text on a malformed
 *  escape (`decodeURIComponent` throws on a lone `%`). Ids like `silver$features` reach the
 *  URL encoded (`silver%24features`), and the raw form is unreadable in a breadcrumb. */
function decode(seg: string): string {
	try {
		return decodeURIComponent(seg);
	} catch {
		return seg;
	}
}

/** A route word for display: dashes to spaces, sentence case, initialisms uppercased. */
function humanise(seg: string): string {
	const decoded = decode(seg);
	// A segment that is not plain lowercase words is an IDENTIFIER — `silver$features`, `A0060198`, a
	// uuid, a table name — and is left EXACTLY as it is, for a pathname whose route is unknown (a
	// 404). Capitalising a dataset id would misreport the data the page is about, which is a worse
	// failure than an ugly crumb. The guard tests the WHOLE label, not just its first character:
	// `gold$catalog` starts with a lowercase letter but is an identifier.
	if (!/^[a-z][a-z-]*$/.test(decoded)) return decoded;
	const spaced = decoded.replace(/-/g, ' ');

	// Uppercase the initialisms, then sentence-case the rest. Per-word TITLE case is still refused —
	// it would render "api docs" as "Api Docs" — so only the first word is capitalised, and only when
	// it is not already an initialism carrying its own casing.
	const words = spaced.split(' ').map((w) => (INITIALISMS.has(w) ? w.toUpperCase() : w));
	const first = words[0]!;
	if (!INITIALISMS.has(first)) words[0] = first[0]!.toUpperCase() + first.slice(1);
	return words.join(' ');
}

/**
 * Crumbs for a domain-relative pathname, in order. Each segment becomes one crumb
 * keyed by its accumulated path prefix (so repeated segments like `/catalog/catalog`
 * stay unique) and carrying the `href` of that prefix, so the trail is navigable —
 * every crumb but the last links back up its own path.
 *
 * `routeId` is the route the pathname matched (`/catalog/tables/[table]`). A segment its route
 * declares as a parameter is a NAME the catalog owns (a project, warehouse, namespace or table id),
 * and its label is the percent-decoded segment, exactly as written: a URL segment alone cannot tell
 * the id `acme-wh` from a route word, and un-dashing or capitalising an id misreports the data the
 * page is about. Every other segment is a route word and is humanised.
 *
 * Route ids pair with the pathname one segment each, after dropping route groups (`(name)`), which
 * name no path segment. Static words and single required parameters (`[id]`) are handled; rest
 * (`[...rest]`) and optional (`[[lang]]`) parameters are not, because they match zero or several
 * segments and the console declares neither.
 */
export function pathCrumbs(pathname: string, routeId: string | null = null): Crumb[] {
	const segs = pathname.split('/').filter(Boolean);
	const routeSegs = routeId?.split('/').filter((seg) => seg && !/^\(.*\)$/.test(seg)) ?? [];
	return segs.map((seg, i) => {
		const id = segs.slice(0, i + 1).join('/');
		const label = routeSegs[i]?.startsWith('[') ? decode(seg) : humanise(seg);
		return { id, label, href: `/${id}` };
	});
}

/** A trail split around an elision point. `lead` + `tail` are what render; `hidden` is what folded
 *  behind the ellipsis affordance (empty when the whole trail fits). The current page is always the
 *  LAST entry of `tail`, so it is the one crumb elision can never take. */
export type CrumbTrail = {
	lead: Crumb[];
	hidden: Crumb[];
	tail: Crumb[];
};

/**
 * Fold the MIDDLE of a trail so a deep path fits a narrow bar. Keeps the first crumb (the app —
 * the anchor that says where you are in the estate) and the last `maxVisible - 1` crumbs (ending
 * at the current page), and hands everything between back as `hidden` for the caller to put behind
 * an ellipsis menu — dropped from the row, never from the DOM, so every ancestor stays reachable.
 *
 * Truncating the tail instead (the CSS-only answer) is what makes a narrow breadcrumb useless: the
 * segment you actually need — the page you are on — is the first thing to disappear.
 *
 * `maxVisible` is clamped to 2 (a lead and a current page); a trail that already fits is returned
 * whole as `tail`.
 */
export function collapseCrumbs(crumbs: Crumb[], maxVisible: number): CrumbTrail {
	const max = Math.max(2, Math.trunc(maxVisible));
	if (crumbs.length <= max) return { lead: [], hidden: [], tail: crumbs };
	const keepTail = max - 1;
	return {
		lead: crumbs.slice(0, 1),
		hidden: crumbs.slice(1, crumbs.length - keepTail),
		tail: crumbs.slice(crumbs.length - keepTail),
	};
}
