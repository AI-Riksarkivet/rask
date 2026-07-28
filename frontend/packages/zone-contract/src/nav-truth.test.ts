import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, svelteBase, zoneDirs } from './manifest';

/**
 * THE NAV TELLS THE TRUTH.
 *
 * A sidebar entry is a promise that a page exists. Nothing checked that promise: an href could
 * point at a route that was never written, or at one that was renamed or deleted underneath it, and
 * the only symptom was a 404 for whoever clicked it. The /lakehouse/data -> /lakehouse/catalog move
 * renamed twenty of them at once, which is exactly the change that would have shipped a dead rail.
 *
 * So: every href in every zone's ZoneNav — group items AND their nested children — must resolve to
 * a real route file, must belong to a zone that actually serves that prefix, and must obey the
 * trailing-slash rule for zone roots.
 */

const ZONES = zoneDirs();

/** zone -> its `paths.base` ('' for the catch-all). */
const BASES = new Map(ZONES.map((z) => [z, svelteBase(z)]));

type Leaf = { zone: string; title: string; href: string; reload: boolean };

/** Every `{ title, href, …, reload? }` literal in a zone's nav config, children included.
 *  Deliberately a source scan rather than an import: nav.ts imports `$app/paths`, which only exists
 *  inside a SvelteKit build, and a test that needs the app's module graph to check a static table
 *  would be more fragile than the thing it guards. */
function leavesOf(zone: string): Leaf[] {
	const path = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'lib', 'nav.ts');
	if (!existsSync(path)) return [];
	const src = readFileSync(path, 'utf8');
	const out: Leaf[] = [];
	// Each leaf carries exactly one `title:` and one `href:`; take them pairwise in source order.
	const re = /title:\s*'([^']+)'[\s\S]{0,200}?href:\s*'([^']+)'([\s\S]{0,200}?)(?=title:\s*'|$)/g;
	for (const m of src.matchAll(re)) {
		out.push({
			zone,
			title: m[1]!,
			href: m[2]!,
			reload: /reload:\s*true/.test(m[3] ?? ''),
		});
	}
	return out;
}

const ALL: Leaf[] = ZONES.flatMap(leavesOf);

/** The zone whose base owns this href — longest base wins; '' (the catch-all) is the fallback. */
function ownerOf(href: string): string {
	let best = '';
	let bestBase = '';
	for (const [zone, base] of BASES) {
		if (!base) continue;
		if (href === base || href.startsWith(base + '/')) {
			if (base.length > bestBase.length) {
				best = zone;
				bestBase = base;
			}
		}
	}
	return best || ZONES.find((z) => BASES.get(z) === '') || 'home';
}

/** Does a SvelteKit route exist for this path inside its owning zone? Accepts a static directory,
 *  a `[param]` directory at that level, or a `[...rest]` catch-all above it. */
function routeExists(zone: string, href: string): boolean {
	const base = BASES.get(zone) ?? '';
	const rest = base && href.startsWith(base) ? href.slice(base.length) : href;
	const segs = rest.split('/').filter(Boolean);
	let dir = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'routes');
	for (const seg of segs) {
		const staticDir = resolve(dir, seg);
		if (existsSync(staticDir)) {
			dir = staticDir;
			continue;
		}
		// A dynamic or rest segment at this level satisfies the leaf.
		if (!existsSync(dir)) return false;
		const dyn = readdirSync(dir, { withFileTypes: true }).find(
			(e) => e.isDirectory() && (e.name.startsWith('[') || e.name.startsWith('[...')),
		);
		if (!dyn) return false;
		dir = resolve(dir, dyn.name);
	}
	return existsSync(resolve(dir, '+page.svelte')) || existsSync(resolve(dir, '+page.ts'));
}

describe('every sidebar href resolves to a real route', () => {
	it('finds leaves to check at all (guards the scanner itself)', () => {
		// A regex that silently matched nothing would make every assertion below vacuously pass.
		expect(
			ALL.length,
			'no nav leaves were parsed — the scanner is broken, not the estate',
		).toBeGreaterThan(30);
	});

	for (const leaf of ALL) {
		it(`${leaf.zone}: "${leaf.title}" -> ${leaf.href}`, () => {
			const owner = ownerOf(leaf.href);
			expect(
				routeExists(owner, leaf.href),
				`${leaf.zone}'s "${leaf.title}" points at ${leaf.href}, which has no route file in the ` +
					`${owner} zone. Either the page was never written, or it moved and this href did not.`,
			).toBe(true);
		});
	}
});

describe('a leaf that leaves its zone declares reload', () => {
	for (const leaf of ALL) {
		const owner = ownerOf(leaf.href);
		if (owner === leaf.zone) continue;
		it(`${leaf.zone}: "${leaf.title}" -> ${leaf.href} (owned by ${owner})`, () => {
			expect(
				leaf.reload,
				`${leaf.zone}'s "${leaf.title}" points into the ${owner} zone but does not declare ` +
					`reload. SvelteKit would soft-navigate into a route this zone does not own -> 404.`,
			).toBe(true);
		});
	}
});

describe('zone-root hrefs keep their trailing slash', () => {
	// Each zone's paths.base serves the trailing form, so a bare `/compute` costs a 308 per hop.
	for (const leaf of ALL) {
		const owner = ownerOf(leaf.href);
		const base = BASES.get(owner) ?? '';
		if (!base || leaf.href !== base) continue;
		it(`${leaf.zone}: "${leaf.title}" -> ${leaf.href} needs a trailing slash when cross-zone`, () => {
			// Only cross-zone links pay the redirect; an in-zone root is resolved by the router.
			if (leaf.zone === owner) return;
			expect(
				leaf.href.endsWith('/'),
				`${leaf.href} is the ${owner} zone's ROOT reached from ${leaf.zone}; without the ` +
					`trailing slash the browser eats a 308 on every hop.`,
			).toBe(true);
		});
	}
});
