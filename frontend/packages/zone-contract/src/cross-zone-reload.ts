/**
 * A cross-zone `<a>` MUST hard-navigate.
 *
 * Each zone is a SEPARATE SvelteKit app. A soft client-router navigation to another zone's path
 * targets a route THIS app's manifest does not contain → 404. Cross-zone links must therefore carry
 * `data-sveltekit-reload` to force a full document navigation. Nothing else catches this: it type-checks,
 * it unit-tests, it renders — it only fails in a browser, on the one hop nobody clicked.
 *
 * This used to be a local ESLint rule (the last thing keeping ESLint installed). It is a gate, not a
 * style preference, so it moved to where the estate's other gates live and now runs on **Svelte's own
 * compiler** — the same parser that builds the component, rather than a second AST from a lint plugin.
 *
 * `lakehouse` is ONE zone spanning the data/lineage/models/admin AREAS: a link between those is
 * same-zone and must NOT hard-navigate, or every area hop pays a document load again. Same-zone links
 * are written `{base}/…`, so their href starts with an opaque expression and is never a zone path.
 */
import { parse } from 'svelte/compiler';

/** The zones with a base path. `home` is the catch-all at '/' and owns no prefix. */
export const ZONES = ['lakehouse', 'explorer', 'annotator', 'compute', 'studio', 'train'];

/** A cross-zone path is domain-relative and single-segment-rooted: `/<zone>` or `/<zone>/…`. */
const ZONE_PATH = new RegExp(`^\\/(${ZONES.join('|')})(?:\\/|$)`);

/**
 * First segments the CATCH-ALL `home` zone owns as real routes.
 *
 * These are the blind spot this list exists to close: `home` has no base path, so a link from another
 * zone to one of its routes looks like an ordinary same-zone absolute href to a gate that only knows
 * zone DIRECTORIES. It is not — `home` is a separate SvelteKit app, so the hop is cross-app and 404s
 * on a soft nav exactly like any zone hop. `/projects` became such a route when the projects surface
 * moved to the main menu (2026-08-03 ruling); the three lakehouse links into it were correct only
 * because a human wrote the attribute, with nothing to catch the next one that forgets. `/settings`
 * joined it when the estate gained a real settings page.
 *
 * MIRRORS `HOME_ROUTES` in `@rask/ui`'s `nav-config.ts`, which states the same fact for the other
 * direction (these are the home zone, so a link to them FROM home stays soft). Two lists because the
 * gate cannot import the shell; a divergence surfaces as a failure here rather than as silence.
 */
const HOME_ROUTES = ['projects', 'settings'];

const HOME_PATH = new RegExp(`^\\/(${HOME_ROUTES.join('|')})(?:\\/|$)`);

/** Opaque-expression placeholder. Cannot contain '/', so it never completes a zone prefix. */
export const EXPR = '￿';

/**
 * True when an href leaves the app it is written in, and so must hard-navigate.
 *
 * `owner` is the zone directory the component lives in. Passing it makes the answer exact in both
 * directions — a `home` component linking to `/projects` is a SAME-app soft nav and must NOT be
 * forced to reload, while any other zone's link to it must. Omit it and the check stays as it was:
 * zone paths only, which is the conservative answer for callers holding a bare source string.
 */
export function isCrossZonePath(path: string | null, owner?: string): boolean {
	if (typeof path !== 'string') return false;
	const zone = path.match(ZONE_PATH)?.[1];
	if (zone !== undefined) return zone !== owner;
	// A home-owned route is cross-app from every zone EXCEPT home — and unknowable without an owner.
	return owner !== undefined && owner !== 'home' && HOME_PATH.test(path);
}

/**
 * Flatten an href attribute value to a comparable string: static text verbatim, every `{…}` as EXPR.
 * Returns null for a shorthand/spread href that cannot be read statically (never flagged).
 */
export function hrefToPath(value: unknown): string | null {
	if (value === true || value == null) return null;
	const nodes = Array.isArray(value) ? value : [value];
	let out = '';
	for (const n of nodes as { type?: string; data?: string; expression?: unknown }[]) {
		if (n.type === 'Text') {
			out += n.data ?? '';
		} else if (n.type === 'ExpressionTag') {
			const e = n.expression as {
				type?: string;
				quasis?: { value: { cooked?: string; raw: string } }[];
			};
			out +=
				e?.type === 'TemplateLiteral'
					? (e.quasis ?? []).map((q) => q.value.cooked ?? q.value.raw).join(EXPR)
					: EXPR;
		} else {
			return null;
		}
	}
	return out;
}

/** `data-sveltekit-reload` present and not explicitly "off". */
function hasReloadEnabled(
	attributes: { type?: string; name?: string; value?: unknown }[],
): boolean {
	for (const a of attributes) {
		if (a.type !== 'Attribute' || a.name !== 'data-sveltekit-reload') continue;
		const v = a.value;
		if (Array.isArray(v) && v.length === 1) {
			const only = v[0] as { type?: string; data?: string };
			if (only.type === 'Text' && only.data === 'off') return false;
		}
		return true; // boolean shorthand, "", or a dynamic value → enabled
	}
	return false;
}

export interface Violation {
	/** The href as written, with expressions rendered as `${…}`. */
	href: string;
	line: number;
}

/** One `<a>` element found in a component: its attributes and 1-based source line. */
export interface Anchor {
	attributes: { type?: string; name?: string; value?: unknown }[];
	line: number;
}

/** Every `<a>` in a component, in source order. Shared by this gate and `link-targets`, which asks a
 *  different question of the same elements (does the target exist) and must not re-derive the walk —
 *  a second traversal would drift, and the blocks ({#if}/{#each}/{#snippet}) are the fiddly part. */
export function walkAnchors(source: string): Anchor[] {
	const ast = parse(source, { modern: true });
	const out: Anchor[] = [];

	const walk = (node: unknown): void => {
		if (!node || typeof node !== 'object') return;
		const n = node as {
			type?: string;
			name?: string;
			attributes?: { type?: string; name?: string; value?: unknown }[];
			fragment?: { nodes?: unknown[] };
			nodes?: unknown[];
			start?: number;
		};
		if (n.type === 'RegularElement' && n.name === 'a') {
			out.push({
				attributes: n.attributes ?? [],
				line: source.slice(0, n.start ?? 0).split('\n').length,
			});
		}
		for (const child of n.fragment?.nodes ?? n.nodes ?? []) walk(child);
		// Blocks ({#if}, {#each}, {#snippet}, …) hold their children in named fragments.
		for (const [key, v] of Object.entries(n)) {
			if (key === 'fragment' || key === 'nodes' || key === 'attributes') continue;
			if (Array.isArray(v)) for (const c of v) walk(c);
			else if (v && typeof v === 'object' && 'type' in (v as object)) walk(v);
		}
	};

	walk(ast.fragment);
	return out;
}

/** Every cross-zone `<a>` in one component that does not hard-navigate. */
export function findViolations(source: string, owner?: string): Violation[] {
	const out: Violation[] = [];
	for (const { attributes, line } of walkAnchors(source)) {
		const href = attributes.find((a) => a.type === 'Attribute' && a.name === 'href');
		if (!href) continue;
		const path = hrefToPath(href.value);
		if (isCrossZonePath(path, owner) && !hasReloadEnabled(attributes)) {
			out.push({ href: path!.replaceAll(EXPR, '${…}'), line });
		}
	}
	return out;
}
