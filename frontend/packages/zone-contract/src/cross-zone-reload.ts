/**
 * Cross-zone link guard.
 *
 * Ported from the retired `ra-local/cross-zone-reload` ESLint rule when the frontend moved to
 * oxlint + oxfmt (oxlint's .svelte support reads the <script> block, not the markup, so an
 * anchor-attribute rule cannot live there). It is a TEST now, which is also where lance-ns keeps
 * its equivalent — so the merge adds cases to this package rather than reintroducing a linter.
 *
 * The hazard: an <a> pointing into ANOTHER microfrontend zone that soft-navigates. SvelteKit's
 * router would try to resolve a route this zone does not own → 404. Such a link must carry
 * `data-sveltekit-reload` so the browser performs a document navigation instead.
 *
 * Same-zone links are written `{base}/…` (a `{base}` expression, never a literal
 * `/default/<domain>`), so they normalise to an opaque placeholder here and are never flagged.
 */

/** Zones that own a base path. `home` is the catch-all at `/` and owns no `/…/<zone>` prefix. */
export const ZONES = ['overview', 'compute', 'discover', 'storage', 'train', 'studio'] as const;

/** Opaque-expression placeholder. Cannot contain a '/', so it never forms a zone path. */
const EXPR = '￿';

const ZONE_PATH = new RegExp(`^/[^/]*/(${ZONES.join('|')})(?:/|$)`);

const ANCHOR = /<a\b([^>]*)>/gis;
const HREF = /\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')/i;
const RELOAD = /\bdata-sveltekit-reload\b/i;

/** Every `{…}` mustache becomes one opaque char, mirroring the original rule's hrefToPath(). */
function normalise(raw: string): string {
	return raw.replace(/\{[^}]*\}/g, EXPR);
}

export function isCrossZonePath(path: string): boolean {
	return ZONE_PATH.test(path);
}

export interface Violation {
	href: string;
	line: number;
}

/** Scan Svelte markup for cross-zone anchors missing `data-sveltekit-reload`. */
export function findViolations(source: string): Violation[] {
	const out: Violation[] = [];
	for (const match of source.matchAll(ANCHOR)) {
		const attrs = match[1] ?? '';
		const href = HREF.exec(attrs);
		// No href, or a fully dynamic `href={…}` we cannot read statically → never flagged.
		if (!href) continue;
		const path = normalise(href[1] ?? href[2] ?? '');
		if (!isCrossZonePath(path)) continue;
		if (RELOAD.test(attrs)) continue;
		out.push({
			href: path,
			line: source.slice(0, match.index).split('\n').length,
		});
	}
	return out;
}
