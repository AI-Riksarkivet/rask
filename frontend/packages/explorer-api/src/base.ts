/**
 * The media-plane API base — where `/api/*` lives for the CURRENT zone.
 *
 * Every media/annotator fetch is same-origin against the zone's OWN BFF proxy routes
 * (`/<zone>/api/*` → viewer/search/annotator, bearer-forwarded), so the path must carry
 * the zone's base. The zones set it once at layout-module init (`setApiBase(base)` with
 * SvelteKit's `$app/paths` base); the default `''` keeps root-absolute `/api/*` for
 * unbased consumers (unit tests, the standalone dev servers before the layout runs).
 * Framework-agnostic on purpose — this package never imports `$app/*`.
 */

let apiBase = '';

/** Set the base path prefixed to every `/api/*` URL this package (and @rask/labeling)
 *  builds — the zone's SvelteKit `base`. Idempotent; trailing slash is normalized.
 *
 *  This is module-level mutable state, which is only safe because a zone is a PROCESS: media and
 *  annotator are separate images, separate pods and separate dev servers, so a server process only
 *  ever sees one base. That invariant is invisible at the call site and would fail as wrong URLs on
 *  a shared SSR request path rather than as an error, so it is asserted instead of assumed —
 *  re-basing to a DIFFERENT non-empty value throws. */
export function setApiBase(b: string): void {
	const next = b.replace(/\/$/, '');
	if (apiBase && next && next !== apiBase) {
		throw new Error(
			`@rask/explorer-api: base already set to "${apiBase}", refusing to re-base to "${next}". ` +
				`Two zones share this module instance — the media plane's URLs cannot be per-request state.`,
		);
	}
	// An empty base is the default, so setting it is never meaningful — and letting it through would
	// let an unbased consumer silently un-base a zone that had already configured itself.
	if (next) apiBase = next;
}

/** Test-only: drop the configured base. Production code must never call this — see setApiBase. */
export function resetApiBase(): void {
	apiBase = '';
}

/** A root-relative media-plane URL under the configured zone base: `apiUrl('/api/health')`
 *  → `/explorer/api/health` once the media zone has called `setApiBase('/explorer')`. */
export function apiUrl(path: string): string {
	return `${apiBase}${path}`;
}
