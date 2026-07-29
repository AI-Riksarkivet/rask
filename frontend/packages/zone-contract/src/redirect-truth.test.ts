import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, svelteBase, zoneDirs } from './manifest';

// A `redirect()` target is a route reference that NO existing gate could see. nav-truth.test.ts walks
// sidebar hrefs; cross-zone-reload.test.ts walks markup anchors. Neither reads a load function, so a
// redirect to a route that was renamed away is invisible to every check while being one of the most
// damaging shapes available: it breaks a URL nobody linked but everybody reaches.
//
// That is exactly what happened. `lakehouse/src/routes/+page.ts` redirected the zone ROOT to
// `${base}/data` after the area was renamed data -> catalog, so `/lakehouse` answered 404 — and because
// the breadcrumb is derived from path segments, the "Lakehouse" crumb on EVERY page beneath it pointed
// at that 404 too. One stale string, dead links across the whole zone, all gates green.

const BASES = new Map(zoneDirs().map((z) => [z, svelteBase(z)] as const));

type Target = { zone: string; file: string; href: string };

/** Every `redirect(<code>, ...)` target in a zone's route files, resolved to a zone-absolute href. */
function redirectsOf(zone: string): Target[] {
	const routes = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'routes');
	if (!existsSync(routes)) return [];
	const out: Target[] = [];

	const walk = (dir: string) => {
		for (const e of readdirSync(dir, { withFileTypes: true })) {
			const p = resolve(dir, e.name);
			if (e.isDirectory()) {
				walk(p);
				continue;
			}
			if (!/^\+(page|layout)(\.server)?\.ts$/.test(e.name)) continue;
			const src = readFileSync(p, 'utf8');
			// `redirect(307, `${base}/catalog`)` and `redirect(307, '/lakehouse/catalog')` both.
			for (const m of src.matchAll(/redirect\(\s*3\d\d\s*,\s*(['"`])([^'"`]+)\1/g)) {
				const raw = m[2]!;
				// Skip anything interpolated beyond the leading ${base} — a computed target is not a
				// static claim about the route tree and cannot be checked here without evaluating it.
				const afterBase = raw.replace(/^\$\{base\}/, '');
				if (afterBase.includes('${')) continue;
				if (/^https?:/.test(afterBase)) continue;
				const base = BASES.get(zone) ?? '';
				const href = raw.startsWith('${base}')
					? `${base}${afterBase}`
					: afterBase.startsWith('/')
						? afterBase
						: null;
				if (href) out.push({ zone, file: p.slice(FRONTEND_ROOT.length + 1), href });
			}
		}
	};
	walk(routes);
	return out;
}

/** Does `href` resolve to a real page in `zone`? Mirrors nav-truth's resolver, including dynamic segments. */
function routeExists(zone: string, href: string): boolean {
	const base = BASES.get(zone) ?? '';
	const rest = base && href.startsWith(base) ? href.slice(base.length) : href;
	const segs = rest.split('/').filter(Boolean);
	let dir = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'routes');
	for (const seg of segs) {
		const staticDir = resolve(dir, seg);
		if (existsSync(staticDir) && statSync(staticDir).isDirectory()) {
			dir = staticDir;
			continue;
		}
		if (!existsSync(dir)) return false;
		const dyn = readdirSync(dir, { withFileTypes: true }).find(
			(e) => e.isDirectory() && e.name.startsWith('['),
		);
		if (!dyn) return false;
		dir = resolve(dir, dyn.name);
	}
	return existsSync(resolve(dir, '+page.svelte')) || existsSync(resolve(dir, '+page.ts'));
}

const ALL = zoneDirs().flatMap(redirectsOf);

describe('every redirect target resolves to a real route', () => {
	it('finds redirects to check at all (guards the scanner itself)', () => {
		// A regex matching nothing would make every assertion below vacuously pass — the failure mode
		// this whole file exists to prevent.
		expect(ALL.length, 'no redirect targets were parsed — the scanner is broken').toBeGreaterThan(0);
	});

	for (const t of ALL) {
		it(`${t.zone}: ${t.file} -> ${t.href}`, () => {
			expect(
				routeExists(t.zone, t.href),
				`${t.file} redirects to ${t.href}, which has no +page.svelte or +page.ts. ` +
					`Anyone landing there gets a 404, and any breadcrumb built from those segments points at it.`,
			).toBe(true);
		});
	}
});
