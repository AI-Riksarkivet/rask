import { existsSync, readFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { resolve } from 'node:path';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

/**
 * The bundle measurement, extracted from `budget.test.ts` so it has ONE implementation.
 *
 * It lived inside the test file, which meant anyone who needed the actual numbers — to decide whether
 * a change fits the remaining headroom, or to justify raising a ceiling — had to either re-implement
 * the closure walk or read them out of an assertion message on a failing run. Both are bad: a second
 * copy drifts, and at that point the printed figures are no longer the gate's figures, which is the
 * entire reason for printing them.
 *
 * So: the test imports this, and `bun src/weigh.ts` prints it. Same code, same answer.
 *
 * Read from Vite's own manifest rather than reconstructed by parsing import statements: the manifest
 * distinguishes `imports` from `dynamicImports` at the source of truth, and a regex over minified
 * output cannot tell `import"./x.js"` from `import("./x.js")` reliably.
 */
export const CLIENT = (zone: string) => `microfrontends/${zone}/.svelte-kit/output/client`;

interface ViteChunk {
	file: string;
	isEntry?: boolean;
	imports?: string[];
	dynamicImports?: string[];
	css?: string[];
}

export interface Weighed {
	staticKB: number;
	deferredKB: number;
	/** Emitted files reachable only through a dynamic import — what the deferred number is made of. */
	deferredFiles: string[];
	staticFiles: string[];
}

/** Gzip a set of files TOGETHER — one stream, the way a browser would receive them over one connection. */
export function gzipKB(dir: string, files: string[]): number {
	if (files.length === 0) return 0;
	const combined = Buffer.concat(files.map((f) => readFileSync(resolve(dir, f))));
	return Math.round(gzipSync(combined, { level: 9 }).length / 1024);
}

/** Split a zone's client output into the static-import closure and everything only reachable lazily.
 *  `null` when there is nothing to weigh (no build in this checkout). */
export function weigh(zone: string): Weighed | null {
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
		// STATIC edges only. Following dynamicImports here would rebuild the very number this replaced.
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

/** The largest single emitted chunk any zone may ship, gzipped. Shared with `budget.test.ts`. */
export const MAX_CHUNK_GZIP_KB = 600;

/** The heaviest single emitted chunk in a zone — the shape that made the annotator's old total lie. */
export function worstChunk(zone: string, weighed: Weighed): { file: string; kb: number } {
	const dir = resolve(FRONTEND_ROOT, CLIENT(zone));
	return [...weighed.staticFiles, ...weighed.deferredFiles]
		.map((file) => ({
			file,
			kb: Math.round(gzipSync(readFileSync(resolve(dir, file)), { level: 9 }).length / 1024),
		}))
		.reduce((a, b) => (b.kb > a.kb ? b : a), { file: '(none)', kb: 0 });
}

// `bun src/weigh.ts` — the reporting entrypoint, alongside the existing `dev:proxy`. Prints headroom
// against budget.json so a change can be judged BEFORE it is committed rather than by a red gate.
if (import.meta.main) {
	const budget = (await import('./budget.json', { with: { type: 'json' } })).default as {
		zones: Record<string, { staticGzipKB: number; deferredGzipKB: number }>;
	};
	const pad = (s: string | number, n: number) => String(s).padStart(n);
	console.log('zone         static  ceil   head   defer  ceil   head   worst chunk');
	for (const zone of zoneDirs()) {
		const w = weigh(zone);
		if (w === null) {
			console.log(`${zone.padEnd(12)} (not built — run \`bun --cwd=frontend run build\`)`);
			continue;
		}
		const lim = budget.zones[zone];
		if (lim === undefined) {
			console.log(`${zone.padEnd(12)} (no entry in budget.json)`);
			continue;
		}
		const worst = worstChunk(zone, w);
		console.log(
			`${zone.padEnd(12)}${pad(w.staticKB, 6)}${pad(lim.staticGzipKB, 6)}${pad(
				lim.staticGzipKB - w.staticKB,
				7,
			)}${pad(w.deferredKB, 8)}${pad(lim.deferredGzipKB, 6)}${pad(
				lim.deferredGzipKB - w.deferredKB,
				7,
			)}   ${worst.kb} KB`,
		);
	}
}
