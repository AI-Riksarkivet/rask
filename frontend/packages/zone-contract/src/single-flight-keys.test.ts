/**
 * A single-flight refresh must name the query instance a SURFACE actually renders.
 *
 * SvelteKit keys a query by its serialized argument, so `listTasks({ projectId })` and
 * `listTasks({ projectId, details: true })` are two different cache entries. A mutation that
 * refreshes the first while every component renders the second resolves against no instance and
 * returns silently — the refresh is decoration, and nothing anywhere says so.
 *
 * The docs name this failure exactly: "if `getPosts({ filter: 'author:santa' })` is rendered on the
 * client, calling `getPosts().refresh()` in the server handler won't update it."
 *
 * It shipped. `annotator/lib/projects/remote/projects.remote.ts`'s `dropTask` refreshed
 * `listTasks({ projectId })`; both render sites — the queue page and the canvas's label stream —
 * hold `{ projectId, details: true }`. It went unnoticed because the caller re-read the page itself
 * afterwards, so the surface updated anyway. Remove that second read (which is the whole point of
 * single-flight) and the miss becomes a stale screen.
 *
 * This is a static gate because there is no runtime signal: a refresh that misses and one that lands
 * are both `void query(...).refresh()` returning undefined. Nothing throws, nothing logs.
 *
 * WHAT IT CHECKS: for every `<fn>({...}).refresh()` inside a `.remote.ts`, the set of argument KEYS
 * must equal the keys some component/route passes to that same `<fn>`. Key SETS, not values — values
 * are runtime (`projectId` is a variable). That is enough to catch the whole class, because the
 * defect is always a missing or extra key, never a wrong value.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const ZONES = join(import.meta.dirname, '../../../microfrontends');

function walk(dir: string, out: string[] = []): string[] {
	for (const entry of readdirSync(dir)) {
		if (entry === 'node_modules' || entry === '.svelte-kit' || entry === 'build') continue;
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) walk(full, out);
		else if (/\.(svelte|ts)$/.test(entry)) out.push(full);
	}
	return out;
}

const FILES = readdirSync(ZONES)
	.filter((z) => statSync(join(ZONES, z)).isDirectory())
	.flatMap((z) => {
		const src = join(ZONES, z, 'src');
		try {
			return walk(src).map((f) => ({ zone: z, file: f, text: readFileSync(f, 'utf8') }));
		} catch {
			return [];
		}
	});

/** The literal object keys in a `name({ ... })` call, or null when the argument is not a literal. */
function keysOfCall(text: string, index: number): string[] | null {
	const open = text.indexOf('(', index);
	if (open < 0) return null;
	let depth = 0;
	let end = -1;
	for (let i = open; i < text.length; i++) {
		if (text[i] === '(') depth++;
		else if (text[i] === ')') {
			depth--;
			if (depth === 0) {
				end = i;
				break;
			}
		}
	}
	if (end < 0) return null;
	const arg = text.slice(open + 1, end).trim();
	if (!arg.startsWith('{')) return null;

	// Split the literal into TOP-LEVEL segments, then take each segment's key. Splitting on `:`
	// directly is wrong and was the first cut's bug: in `{ namespace: current }` the trailing token
	// after the colon is the VALUE, and treating it as a shorthand key made every `key: value` call
	// look like it passed two keys — which reported five false mismatches in a zone that was correct.
	const inner = arg.slice(1, -1);
	const segments: string[] = [];
	let nest = 0;
	let token = '';
	for (const ch of inner) {
		if ('{[('.includes(ch)) nest++;
		else if ('}])'.includes(ch)) nest--;
		if (ch === ',' && nest === 0) {
			segments.push(token);
			token = '';
			continue;
		}
		token += ch;
	}
	segments.push(token);

	const keys = segments
		.map((seg) => {
			const trimmed = seg.trim();
			if (!trimmed) return '';
			if (trimmed.startsWith('...')) return '…'; // a spread: shape is not statically known
			// `key: value` → key; `key` → key. Only a TOP-LEVEL colon separates them.
			let d = 0;
			for (let i = 0; i < trimmed.length; i++) {
				const c = trimmed[i] as string;
				if ('{[('.includes(c)) d++;
				else if ('}])'.includes(c)) d--;
				else if (c === ':' && d === 0) return trimmed.slice(0, i).trim();
			}
			return trimmed;
		})
		.filter((k) => k !== '');

	// A shape containing a spread is not statically comparable — refuse to judge it rather than
	// judge it wrongly.
	return keys.includes('…') ? null : keys;
}

/** Every `X({...}).refresh()` inside a `.remote.ts`, with the keys it passed.
 *
 * WALKS BACKWARDS FROM `.refresh()` rather than matching the whole call with a regex. The regex form
 * was `(\w+)\(\s*\{[^)]*\}\s*\)\s*\.refresh\(\)`, and `[^)]*` cannot cross a `)` — so any key
 * literal containing a CALL (`fetchAccess({ id: currentId() })`) ended the match early and the site
 * was invisible. Measured: **40 `.refresh()` sites in the estate's `.remote.ts` files, 15 matched.**
 * The gate judged 37% of the sites while its own anti-vacuity guard passed, which is the shape this
 * whole audit is about — a guard counts what the scanner FOUND, so it can never see what the scanner
 * missed.
 *
 * A backward walk has no window to tune and no nesting it cannot cross: find `.refresh()`, step back
 * over the balanced `(...)`, then take the identifier in front of it. `keysOfCall` already parses the
 * argument with a depth counter, so it was never the limiting half.
 */
function refreshCalls() {
	const found: { zone: string; file: string; fn: string; keys: string[] }[] = [];
	for (const { zone, file, text } of FILES) {
		if (!file.endsWith('.remote.ts')) continue;
		for (const m of text.matchAll(/\.refresh\(\s*\)/g)) {
			let i = m.index - 1;
			while (i >= 0 && /\s/.test(text[i] as string)) i--;
			// `foo.refresh()` on a plain variable is not a keyed call and is correctly skipped.
			if (text[i] !== ')') continue;

			let depth = 0;
			let open = -1;
			for (let j = i; j >= 0; j--) {
				const c = text[j] as string;
				if (c === ')') depth++;
				else if (c === '(') {
					depth--;
					if (depth === 0) {
						open = j;
						break;
					}
				}
			}
			if (open < 0) continue;

			let k = open - 1;
			while (k >= 0 && /\s/.test(text[k] as string)) k--;
			const end = k + 1;
			while (k >= 0 && /[\w$]/.test(text[k] as string)) k--;
			const fn = text.slice(k + 1, end);
			if (!fn) continue;

			const keys = keysOfCall(text, open);
			if (keys) found.push({ zone, file, fn, keys });
		}
	}
	return found;
}

/** Every call to `fn` from a component or route in the same zone, with the keys it passed. */
function renderSiteKeys(zone: string, fn: string): string[][] {
	const shapes: string[][] = [];
	for (const f of FILES) {
		if (f.zone !== zone || f.file.endsWith('.remote.ts')) continue;
		for (const m of f.text.matchAll(new RegExp(`\\b${fn}\\(\\s*\\{`, 'g'))) {
			const keys = keysOfCall(f.text, m.index + fn.length);
			if (keys) shapes.push(keys);
		}
	}
	return shapes;
}

const sorted = (k: string[]) => [...k].sort().join(',');

describe('single-flight refreshes name a cache key something renders', () => {
	it('finds refresh calls to check — the scanner must not silently match nothing', () => {
		// The guard that keeps every assertion below from being vacuous. A regex that stops matching
		// (a formatting change, a renamed method) would otherwise turn this suite green while
		// checking nothing at all — the failure mode that is quieter than a red gate.
		expect(refreshCalls().length).toBeGreaterThan(0);
	});

	it('every refreshed query is refreshed with a shape some surface holds', () => {
		const mismatches: string[] = [];

		for (const call of refreshCalls()) {
			const shapes = renderSiteKeys(call.zone, call.fn);
			// No render site at all is not this test's business — the query may be consumed only by
			// another remote function, or the call may be the sole consumer.
			if (shapes.length === 0) continue;
			if (shapes.some((s) => sorted(s) === sorted(call.keys))) continue;
			mismatches.push(
				`${call.zone}: ${call.fn}({${call.keys.join(', ')}}).refresh() matches no render site ` +
					`— surfaces use ${shapes.map((s) => `{${s.join(', ')}}`).join(' | ')}`,
			);
		}

		expect(mismatches).toEqual([]);
	});
});
