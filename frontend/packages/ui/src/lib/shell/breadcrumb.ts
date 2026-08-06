/**
 * Breadcrumb + project derivation for the shared shell — the single source of
 * truth so `app-shell`, `project-switcher`, and any future chrome can't drift.
 *
 * Project-first IA via HOST: the project IS the request host (e.g. demo.localhost),
 * so the pathname carries only the domain + in-domain trail. Every path segment is
 * a crumb (the DOMAIN is the first crumb); the project is the breadcrumb ROOT and is
 * derived from the host, never from the path.
 */

export type Crumb = {
	id: string;
	label: string;
	/** The absolute, domain-relative path this crumb points at (`/data/tables`). */
	href: string;
};

/** Percent-decode a path segment for display, falling back to the raw text on a malformed
 *  escape (`decodeURIComponent` throws on a lone `%`). Ids like `silver$features` reach the
 *  URL encoded (`silver%24features`), and the raw form is unreadable in a breadcrumb. */
function humanise(seg: string): string {
	let decoded = seg;
	try {
		decoded = decodeURIComponent(seg);
	} catch {
		// keep the raw segment
	}
	const spaced = decoded.replace(/-/g, ' ');
	// Sentence-case the label, so a trail reads "Lakehouse > Catalog > Storage" instead of shouting
	// lowercase. Only the FIRST character, and only when it is a lowercase ASCII letter:
	//   - per-word title case would render "api docs" as "Api Docs", which is worse than either;
	//   - a segment starting with anything else is an IDENTIFIER — `silver$features`, `A0060198`, a
	//     uuid, a table name — and is left EXACTLY as it is. Capitalising a dataset id would
	//     misreport the data the page is about, which is a worse failure than an ugly crumb.
	// The guard tests the WHOLE label, not just its first character: `gold$catalog` starts with a
	// lowercase letter but is an identifier, and a first-char-only check rendered it `Gold$catalog`.
	return /^[a-z][a-z ]*$/.test(spaced) ? spaced[0]!.toUpperCase() + spaced.slice(1) : spaced;
}

/**
 * Crumbs for a domain-relative pathname, in order. Each segment becomes one crumb
 * keyed by its accumulated path prefix (so repeated segments like `/studio/studio`
 * stay unique) and carrying the `href` of that prefix, so the trail is navigable —
 * every crumb but the last links back up its own path. The human label drops dashes
 * and percent-decodes. The domain (first segment) is kept — it is the first crumb,
 * not the project.
 */
export function pathCrumbs(pathname: string, zoneLabels: Record<string, string> = {}): Crumb[] {
	const segs = pathname.split('/').filter(Boolean);
	return segs.map((seg, i) => {
		const id = segs.slice(0, i + 1).join('/');
		// The FIRST segment is the zone, and its human name is the NAVBAR's, not the directory's.
		// Reported: the trail read "Annotator > Browse" while the bar above it said "Annotate" — the
		// same zone under two names on one screen. `humanise` can only ever produce the directory
		// name, because that is all a URL segment carries; the label lives in `topNav()`. Later
		// segments keep `humanise`: they are in-zone paths and identifiers, which have no nav entry.
		const label = i === 0 ? (zoneLabels[seg] ?? humanise(seg)) : humanise(seg);
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
 * Fold the MIDDLE of a trail so a deep path fits a narrow bar. Keeps the first crumb (the zone —
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

/**
 * The project label carried by a request host (e.g. `demo.localhost` -> `demo`),
 * or `null` when the host has none. Only a real subdomain is a project label: bare
 * hosts (`localhost`) and IPv4 hosts (`127.0.0.1` — a numeric first label is an
 * octet) have no project.
 */
export function projectFromHost(host: string): string | null {
	const labels = host.split('.');
	if (labels.length > 1 && !/^\d+$/.test(labels[0] ?? '')) {
		return labels[0] || null;
	}
	return null;
}
