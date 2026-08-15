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
	const path = zoneNavPath(zone);
	if (!existsSync(path)) return [];
	return scanLeaves(readFileSync(path, 'utf8'), zone);
}

const zoneNavPath = (zone: string) =>
	resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'lib', 'nav.ts');

/** The estate navbar — a nav surface in its own right, rendered in all seven zones. */
const SHELL_NAV = resolve(FRONTEND_ROOT, 'packages/ui/src/lib/shell/nav-config.ts');

/** Every nav source this gate reads: each zone's sidebar, plus the shared navbar. */
const NAV_SOURCES: { label: string; path: string }[] = [
	...ZONES.map((z) => ({ label: z, path: zoneNavPath(z) })).filter((s) => existsSync(s.path)),
	{ label: 'shell', path: SHELL_NAV },
];

/** One object literal's OWN text — nested literals, comments and string bodies removed. */
type Frame = { start: number; own: string };

/**
 * Split a source file into object literals, each carrying only the text at its OWN brace depth.
 *
 * THIS REPLACED A BOUNDED-WINDOW REGEX, and the replacement is the point. The old scan paired each
 * `title:` with the first `href:` after it, then had to lazily reach a `(?=title:)` lookahead within
 * a fixed window — so a leaf whose gap to the NEXT leaf exceeded the window was dropped whole. The
 * window went 200 -> 900 once, which moved the threshold and left the defect; measured at 900, ten
 * hrefs were still invisible across four files (compute's `/compute/gpu` + `/compute/serve`,
 * lakehouse's `/lakehouse/lineage/columns` + `/lakehouse/workbench`, explorer's `/explorer/graph`,
 * and the shell's `/explorer/workflow`, `/models/runs`, `/lakehouse/admin/dlq` and — rendered in all
 * seven zones — `/` and `/settings`).
 *
 * Worse than dropping, it MIS-PAIRED: compute's ZoneNav carries its own `title: 'Compute'` above the
 * `root` leaf, so the scan glued that label to the Overview leaf's href and reported a leaf named
 * "Compute" that does not exist — a failure here would have named the wrong row.
 *
 * A frame walk has no window to tune. The walk consumes comments and string bodies as it goes, which
 * is load-bearing twice over: `models/nav.ts` documents this very regex and contains the text
 * "`href:`" in prose, and `nav-config.ts` declares TypeScript members `href: string;` inside `{…}`
 * type literals. Both would be counted by a naive scan; neither is a leaf.
 */
function objectFrames(src: string): Frame[] {
	const frames: Frame[] = [];
	const stack: { start: number; own: string }[] = [];
	const push = (s: string) => {
		const top = stack.at(-1);
		if (top) top.own += s;
	};
	let i = 0;
	while (i < src.length) {
		const c = src[i]!;
		const n = src[i + 1];
		if (c === '/' && n === '/') {
			while (i < src.length && src[i] !== '\n') i++;
			continue;
		}
		if (c === '/' && n === '*') {
			i += 2;
			while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) i++;
			i += 2;
			continue;
		}
		if (c === "'" || c === '"' || c === '`') {
			const start = i++;
			while (i < src.length && src[i] !== c) i += src[i] === '\\' ? 2 : 1;
			i++;
			push(src.slice(start, i)); // the body is kept — it holds the title/href VALUES
			continue;
		}
		if (c === '{') {
			stack.push({ start: i, own: '' });
			i++;
			continue;
		}
		if (c === '}') {
			const done = stack.pop();
			if (done) frames.push(done); // popped, NOT merged up: own-depth only
			i++;
			continue;
		}
		push(c);
		i++;
	}
	return frames.sort((a, b) => a.start - b.start);
}

/** Every `href: '…'` value in the file's CODE — the set the frame scan must reproduce exactly. */
function hrefLiterals(src: string): string[] {
	// Same walk, so comments and the `href: string;` type members are excluded on the same terms.
	const code = objectFrames(src)
		.map((f) => f.own)
		.join('\n');
	return [...code.matchAll(/href:\s*'([^']+)'/g)].map((m) => m[1]!);
}

/** The leaf scan, shared with the SHELL navbar check below — one parser for both nav surfaces, so
 *  they cannot drift in what the gate can see. A leaf is any object literal declaring its own
 *  quoted `href`; its `title` and `reload` are read at that same depth, so a nested object can
 *  neither donate nor steal them. */
function scanLeaves(src: string, zone: string): Leaf[] {
	const out: Leaf[] = [];
	for (const frame of objectFrames(src)) {
		const href = /href:\s*'([^']+)'/.exec(frame.own);
		if (!href) continue;
		const title = /title:\s*'([^']+)'/.exec(frame.own);
		out.push({
			zone,
			// An href with no title at its own depth is still a promise a route exists, so it is
			// checked; it just cannot be named. None exist today — if one appears, the route
			// assertion below still covers it.
			title: title?.[1] ?? `(untitled ${href[1]})`,
			href: href[1]!,
			reload: /reload:\s*true/.test(frame.own),
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

describe('the scanner sees every href (guards the gate itself)', () => {
	// THIS is what the old `> 30` floor was standing in for, and why the floor could not do the job:
	// 90 hrefs exist, the floor asked for 30, and the scan found 80 — a 10-href hole that no
	// assertion could ever notice because a floor cannot know what it is missing. Self-consistency
	// can: the parse must reproduce the file's own href literals exactly, so a scanner regression is
	// reported as the specific hrefs that fell out, in the file they fell out of.
	it.each(NAV_SOURCES.map((s) => [s.label, s.path] as const))(
		'%s: the scan reproduces every href literal in the source',
		(label, path) => {
			const src = readFileSync(path, 'utf8');
			const scanned = scanLeaves(src, label);
			const declared = hrefLiterals(src);
			const seen = new Set(scanned.map((l) => l.href));
			const missed = declared.filter((h) => !seen.has(h));
			expect(
				scanned.length,
				`the ${label} nav declares ${declared.length} href literals but the scan produced ` +
					`${scanned.length}. Unchecked: ${missed.join(', ') || '(a duplicate href, not a miss)'}. ` +
					`Every one is a sidebar promise no assertion below covers.`,
			).toBe(declared.length);
		},
	);
});

describe('every sidebar href resolves to a real route', () => {
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
	const crossZone = ALL.filter((leaf) => ownerOf(leaf.href) !== leaf.zone);
	// The set can legitimately be empty (it is, since media's Annotate row left the sidebar — the
	// top navbar owns cross-zone hops and applies reload itself) — vitest fails an empty describe,
	// so pin the emptiness as the passing case instead.
	if (crossZone.length === 0) {
		it('no sidebar leaf leaves its zone — the top navbar owns every cross-zone hop', () => {
			expect(crossZone).toEqual([]);
		});
	}
	for (const leaf of crossZone) {
		const owner = ownerOf(leaf.href);
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

describe('every SHELL navbar href resolves to a real route', () => {
	// The estate navbar (`@rask/ui`'s nav-config.ts) renders in all seven zones — and sat outside
	// every resolution gate: `link-targets` deliberately checks only a link's FIRST segment, which is
	// exactly how `/models/playground` stayed a live navbar 404 after #131 moved inference out (the
	// removal comment in nav-config.ts:293-301 names this gap verbatim). Same scanner, same resolver
	// as the zone sidebars above, so the two nav surfaces cannot drift in what the gate can see.
	const shellLeaves = scanLeaves(readFileSync(SHELL_NAV, 'utf8'), 'shell').filter((leaf) =>
		leaf.href.startsWith('/'),
	);

	it.each(shellLeaves.map((l) => [l.title, l.href] as const))(
		'shell: "%s" -> %s',
		(title, href) => {
			const owner = ownerOf(href);
			expect(
				routeExists(owner, href),
				`the estate navbar links "${title}" to ${href}, but the ${owner || 'home'} zone serves no ` +
					`such route — a navbar 404 in all seven zones.`,
			).toBe(true);
		},
	);
});
