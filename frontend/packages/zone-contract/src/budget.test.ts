import { existsSync, readFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, zoneDirs } from './manifest';
import budget from './budget.json' with { type: 'json' };

/**
 * The composed-page bundle budget.
 *
 * Micro-frontends make bundle regressions invisible: each zone looks fine on its own, nobody measures
 * the estate, and the cost only shows up as a slow cross-zone hop. That gets worse now that the zones
 * ship on independent tags — no single release looks at all four. So the ceiling is committed, and
 * raising it is an edit someone has to justify rather than a number that drifts.
 *
 * MEASURED IN TWO HALVES, and that distinction is the whole point of this gate.
 *
 * The first version gzipped every emitted .js/.css together and called the result "what the browser
 * pays to enter a zone". For three zones that was near enough. For the annotator it was false by an
 * order of magnitude: 3809 of its 4179 KB was ONE chunk — a vendored OpenCV wasm build — reached only
 * through a dynamic `import()` inside the magnetic tool, so a session that never picked that tool never
 * downloaded a byte of it. The consequences were worse than the wrong number:
 *
 *   - The annotator read as 4179/4800 KB — 87% of budget "used" — while its real entry cost was 365 KB.
 *     A change that doubled the entry graph would have passed the gate without a murmur, because 400 KB
 *     is noise next to a 3.8 MB constant. The gate could not see the regression it exists to catch.
 *   - It made the estate's shape unreadable. On entry cost the annotator is the SECOND-LIGHTEST zone
 *     (365 KB, against media's 929) — the opposite of what the old number said, and the opposite of
 *     what budget.json's own note claimed.
 *
 * So: `staticGzipKB` is the closure of STATIC imports from every entry Vite emitted (SvelteKit marks
 * entry/start, entry/app and every route node as entries), plus their CSS — code the zone can load
 * without a user action, i.e. what crossing the boundary actually costs. `deferredGzipKB` is
 * everything else: real bytes, but ones the user opts into. Both are ceilinged, because an unbounded
 * lazy payload is still a problem — just a different problem, on a different budget.
 *
 * That split is also what made the 3.8 MB legible enough to kill (#117). Measured against the live
 * deployment, no session ever fetched it: the wasm bought exactly two calls, `cvtColor(RGBA2GRAY)` and
 * `cornerMinEigenVal(5, 3, BORDER_DEFAULT)`, now ~120 lines in `@rask/engine`'s `tools/corners.ts` and
 * pinned to OpenCV's own recorded output. The annotator's deferred half went 3854 KB → 46 KB with its
 * entry graph unchanged at 365 KB. So the gate below no longer asks whether the blob is lazy; it asks
 * that no zone acquires one at all, by name and by size.
 *
 * Read from Vite's own manifest rather than reconstructed by parsing import statements: the manifest
 * distinguishes `imports` from `dynamicImports` at the source of truth, and a regex over minified
 * output cannot tell `import"./x.js"` from `import("./x.js")` reliably.
 */
const CLIENT = (zone: string) => `microfrontends/${zone}/.svelte-kit/output/client`;

interface ViteChunk {
	file: string;
	isEntry?: boolean;
	imports?: string[];
	dynamicImports?: string[];
	css?: string[];
}

interface Weighed {
	staticKB: number;
	deferredKB: number;
	/** Emitted files reachable only through a dynamic import — what the deferred number is made of. */
	deferredFiles: string[];
	staticFiles: string[];
}

/** Gzip a set of files TOGETHER — one stream, the way a browser would receive them over one connection. */
function gzipKB(dir: string, files: string[]): number {
	if (files.length === 0) return 0;
	const combined = Buffer.concat(files.map((f) => readFileSync(resolve(dir, f))));
	return Math.round(gzipSync(combined, { level: 9 }).length / 1024);
}

/** Split a zone's client output into the static-import closure and everything only reachable lazily.
 *  `null` when there is nothing to weigh (no build in this checkout). */
function weigh(zone: string): Weighed | null {
	const dir = resolve(FRONTEND_ROOT, CLIENT(zone));
	const manifestPath = resolve(dir, '.vite/manifest.json');
	if (!existsSync(manifestPath)) return null;
	const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as Record<string, ViteChunk>;
	const chunks = Object.values(manifest);
	const byFile = new Map(chunks.map((c) => [c.file, c]));
	/** A chunk's `imports` name manifest KEYS, not emitted files — resolve both spellings. */
	const emitted = (ref: string) => manifest[ref]?.file ?? ref;

	const reached = new Set<string>();
	const css = new Set<string>();
	const stack = chunks.filter((c) => c.isEntry).map((c) => c.file);
	while (stack.length > 0) {
		const file = stack.pop()!;
		if (reached.has(file)) continue;
		reached.add(file);
		const chunk = byFile.get(file);
		for (const sheet of chunk?.css ?? []) css.add(sheet);
		// STATIC edges only. Following dynamicImports here would rebuild the very number this gate replaced.
		for (const ref of chunk?.imports ?? []) {
			const target = emitted(ref);
			if (!reached.has(target)) stack.push(target);
		}
	}

	const staticFiles = [...new Set([...reached, ...css])].sort();
	const all = new Set<string>();
	for (const chunk of chunks) {
		all.add(chunk.file);
		for (const sheet of chunk.css ?? []) all.add(sheet);
	}
	const deferredFiles = [...all].filter((f) => !staticFiles.includes(f)).sort();

	return {
		staticKB: gzipKB(dir, staticFiles),
		deferredKB: gzipKB(dir, deferredFiles),
		deferredFiles,
		staticFiles,
	};
}

const zones = zoneDirs();
type Zone = { staticGzipKB: number; deferredGzipKB: number };
const limits = budget.zones as Record<string, Zone>;

describe('every zone has a declared budget', () => {
	it.each(zones)('%s', (zone) => {
		expect(Object.keys(limits), `add ${zone} to budget.json`).toContain(zone);
	});

	it('declares no budget for a zone that no longer exists', () => {
		expect(Object.keys(limits).sort()).toEqual([...zones].sort());
	});
});

describe('the code a zone loads on entry stays inside its budget', () => {
	it.each(zones)('%s', (zone) => {
		const weighed = weigh(zone);
		if (weighed === null) {
			// Only reachable from a bare `vitest run` in a checkout that has never been built. Under turbo
			// there is always something to weigh: `@rask/zone-contract#test` dependsOn every `<zone>#build`,
			// and manifest.test.ts (`depends on every zone build, and only those`) fails if that list drifts.
			// That pairing is what keeps these ceilings from going quietly vacuous — an unmeasurable zone
			// must not fail the gate, but a missing build dependency must.
			return;
		}
		const limit = limits[zone]!.staticGzipKB;
		expect(
			weighed.staticKB,
			`${zone} loads ${weighed.staticKB} KB gzipped on entry, over its ${limit} KB budget. ` +
				`Cut it, defer it behind a dynamic import, or raise the number in budget.json WITH the ` +
				`reason in the commit message.`,
		).toBeLessThanOrEqual(limit);
	});
});

describe('lazily-loaded payloads stay inside their own budget', () => {
	it.each(zones)('%s', (zone) => {
		const weighed = weigh(zone);
		if (weighed === null) return;
		const limit = limits[zone]!.deferredGzipKB;
		expect(
			weighed.deferredKB,
			`${zone} ships ${weighed.deferredKB} KB gzipped behind dynamic imports, over its ${limit} KB ` +
				`budget. Deferred is not free — someone waits for it the first time they use the feature.`,
		).toBeLessThanOrEqual(limit);
	});
});

describe('no zone vendors OpenCV again', () => {
	// This test used to assert the opposite — that the OpenCV chunk was present, and lazy. It was
	// deleted in #117 (two calls reimplemented in @rask/engine/tools/corners.ts), so the invariant
	// inverts: the dependency is gone and re-adding it is the regression. Checked by name because
	// the size gate below is a blunt instrument and the next person needs the cause, not a number:
	// the same two ops are already in the repo, so a reappearing `opencv` means someone reached for
	// 3.9 MB gzipped that we have already proven unnecessary.
	it.each(zoneDirs())('%s', (zone) => {
		const weighed = weigh(zone);
		if (weighed === null) return;
		const dir = resolve(FRONTEND_ROOT, CLIENT(zone));
		const carriesOpenCv = (f: string) =>
			f.endsWith('.js') && readFileSync(resolve(dir, f), 'utf8').includes('opencv');
		expect(
			[...weighed.staticFiles, ...weighed.deferredFiles].filter(carriesOpenCv),
			`${zone} vendors OpenCV again. The magnetic tool's two ops (cvtColor RGBA→GRAY and ` +
				`cornerMinEigenVal) live in @rask/engine's tools/corners.ts, pinned to OpenCV's own ` +
				`recorded output — use those instead of re-adding 3.9 MB gzipped, lazy or not.`,
		).toEqual([]);
	});
});

/** The largest single emitted chunk any zone may ship, gzipped. Media's atlas route is the estate's
 *  worst at 504 KB (embedding-atlas + d3); everything else is under 120 KB. 600 KB leaves that room
 *  and nothing like enough for a vendored runtime — the class of thing this catches is megabytes. */
const MAX_CHUNK_GZIP_KB = 600;

describe('no single chunk dominates its zone', () => {
	// The failure mode the two-halves split exposed, generalised. One vendored monolith is what made
	// the annotator's total meaningless, and "it's behind a dynamic import" did not make it free —
	// somebody still waited 1.6–4.3 s for it the first time. A per-chunk ceiling catches that shape
	// wherever it lands, in either half, before it swamps a zone's total the way OpenCV did.
	it.each(zoneDirs())('%s', (zone) => {
		const weighed = weigh(zone);
		if (weighed === null) return;
		const dir = resolve(FRONTEND_ROOT, CLIENT(zone));
		const worst = [...weighed.staticFiles, ...weighed.deferredFiles]
			.map((file) => ({
				file,
				kb: Math.round(gzipSync(readFileSync(resolve(dir, file)), { level: 9 }).length / 1024),
			}))
			.reduce((a, b) => (b.kb > a.kb ? b : a), { file: '(none)', kb: 0 });
		expect(
			worst.kb,
			`${zone} ships ${worst.file} at ${worst.kb} KB gzipped in one chunk, over the ` +
				`${MAX_CHUNK_GZIP_KB} KB per-chunk ceiling. A blob that size is a vendored runtime, not ` +
				`application code — split it, or replace the handful of calls that pull it in.`,
		).toBeLessThanOrEqual(MAX_CHUNK_GZIP_KB);
	});
});

describe('the annotator is not the heavy zone the budget once assumed', () => {
	it('costs less to enter than media, and no longer hides weight behind a lazy import', () => {
		// Both halves are now measured, and both say the same thing: the annotator is light. Its old
		// justification-by-weight ("it defers 3.8 MB a searcher is spared") is gone with the OpenCV
		// removal — its deferred half is 46 KB, the same as media's and lakehouse's. The zone split
		// stands on ownership and independent deploys, not bytes, and this test says so rather than
		// asserting a size relationship that no longer holds.
		const a = weigh('annotator');
		const m = weigh('media');
		if (a === null || m === null) return;
		expect(a.staticKB).toBeLessThan(m.staticKB);
		expect(
			a.deferredKB,
			'the annotator is deferring a large payload again — has a heavy dependency crept back in?',
		).toBeLessThan(200);
	});
});
