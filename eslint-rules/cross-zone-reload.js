// --- Local ESLint rule: cross-zone <a> links must hard-navigate -------------
// Project-first IA via HOST: each domain (`/<domain>`) is a SEPARATE SvelteKit
// microfrontend app (the project is the request host, not a path segment). A soft
// client-router nav to another domain's path targets a route THIS app doesn't own
// → 404. Cross-zone <a>s must therefore set `data-sveltekit-reload` to force a full
// document navigation. The shared shell (`@rask/ui/shell` nav-main) does this
// dynamically; this rule guards the hand-written links in app pages so the
// convention can't silently drift. Same-zone links use `{base}/…` (a `{base}`
// expression, never a literal `/<domain>`), so they read as an opaque placeholder
// here and are ignored.
const ZONES = ['overview', 'compute', 'discover', 'storage', 'train', 'studio'];
// A cross-zone path is domain-relative and single-segment-rooted: `/<domain>` or
// `/<domain>/…`. (The retired `/default/<domain>` two-segment form is intentionally
// NOT matched — the leading throwaway segment is gone under host-based routing.)
const ZONE_PATH = new RegExp(`^\\/(${ZONES.join('|')})(?:\\/|$)`);
const EXPR = '￿'; // opaque-expression placeholder (can't contain a '/')

/** True when an href path targets another microfrontend zone (must hard-navigate). */
export function isCrossZonePath(path) {
	return typeof path === 'string' && ZONE_PATH.test(path);
}

// Reconstruct a comparable path string from an href attribute's value nodes.
// Static text is kept verbatim; every `${…}`/`{…}` becomes EXPR. Returns null for
// shorthand/spread hrefs we can't read statically (never flagged).
function hrefToPath(valueNodes) {
	if (!valueNodes || valueNodes.length === 0) return null;
	let out = '';
	for (const n of valueNodes) {
		if (n.type === 'SvelteLiteral') {
			out += n.value;
		} else if (n.type === 'SvelteMustacheTag') {
			const e = n.expression;
			if (e && e.type === 'TemplateLiteral') {
				out += e.quasis.map((q) => q.value.cooked ?? q.value.raw).join(EXPR);
			} else {
				out += EXPR;
			}
		} else {
			return null;
		}
	}
	return out;
}

// `data-sveltekit-reload` present and not explicitly "off".
function hasReloadEnabled(attrs) {
	for (const a of attrs) {
		if (a.type === 'SvelteAttribute' && a.key?.name === 'data-sveltekit-reload') {
			const v = a.value;
			if (v?.length === 1 && v[0].type === 'SvelteLiteral' && v[0].value === 'off') return false;
			return true; // boolean shorthand, "", or a dynamic value → enabled
		}
	}
	return false;
}

/** The `cross-zone-reload` rule object (ESLint flat-config `rules` entry). */
const crossZoneReload = {
	meta: {
		type: 'problem',
		docs: {
			description:
				'Cross-zone <a> links (into another microfrontend /<domain>) must set data-sveltekit-reload.',
		},
		messages: {
			missingReload:
				'Cross-zone link to "{{href}}" must set data-sveltekit-reload — a soft client nav targets a route this microfrontend zone does not own (→ 404). Add data-sveltekit-reload, or use {base}/… for a same-zone link.',
		},
		schema: [],
	},
	create(context) {
		return {
			SvelteElement(node) {
				const nm = node.name;
				const tag = typeof nm === 'string' ? nm : nm?.name;
				if (tag !== 'a') return;
				const attrs = node.startTag?.attributes ?? [];
				const hrefAttr = attrs.find(
					(a) => a.type === 'SvelteAttribute' && a.key?.name === 'href',
				);
				if (!hrefAttr) return;
				const path = hrefToPath(hrefAttr.value);
				if (!isCrossZonePath(path)) return;
				if (hasReloadEnabled(attrs)) return;
				context.report({
					node,
					messageId: 'missingReload',
					data: { href: path.replaceAll(EXPR, '${…}') },
				});
			},
		};
	},
};

/** Plugin wrapper for flat config: `plugins: { 'ra-local': raLocal }`. */
export const raLocal = { rules: { 'cross-zone-reload': crossZoneReload } };
