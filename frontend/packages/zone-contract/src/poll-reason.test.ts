import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

/**
 * Every remaining `setInterval` in product code must say why it is still there.
 *
 * This zone went from 13 polling timers to 1 by moving each feed onto a cursor, and the surviving one is
 * legitimate: it renders a Prometheus rate over a MOVING five-minute window, whose value decays as the
 * clock advances even when nothing happens, so there is no event that means "the window moved". A second
 * legitimate survivor polls service liveness, where the transition worth catching is a recovery and nothing
 * publishes "the thing you could not reach is up".
 *
 * Both are defensible. Neither is discoverable — and a count in a commit message is not a gate: the next
 * timer added lands silently and the honest answer to "are the timers gone?" quietly becomes "mostly".
 * So the rule is not "no timers", which would be a lie the estate cannot keep. It is: a file that polls
 * carries `POLL REASON:` and states it, and this test fails on a file that does not.
 *
 * Deliberately a marker rather than prose-matching: a reviewer can grep the whole estate for every
 * deliberate poll and read the arguments side by side.
 */
const MARKER = 'POLL REASON:';

/** Every `.ts`/`.svelte` under a directory, recursively. */
function sources(dir: string, acc: string[] = []): string[] {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.name === 'node_modules' || entry.name === '.svelte-kit') continue;
		const path = join(dir, entry.name);
		if (entry.isDirectory()) sources(path, acc);
		else if (/\.(ts|svelte)$/.test(entry.name)) acc.push(path);
	}
	return acc;
}

/** Comments stripped first, then a real call looked for.
 *
 *  A lookbehind cannot do this: the check runs over a whole file, so any `//` anywhere earlier in it
 *  matches and every call reads as commented. The first version of this gate reported two files whose only
 *  mention of the word was prose EXPLAINING the migration away from timers — a false positive that would
 *  have taught the next reader to distrust the gate. */
const withoutComments = (body: string): string =>
	body.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1');

const callsIt = (body: string): boolean => /\bsetInterval\s*\(/.test(withoutComments(body));

describe('every deliberate poll states its reason', () => {
	it('no file calls setInterval without a POLL REASON', () => {
		const offenders: string[] = [];
		for (const zone of zoneDirs()) {
			for (const file of sources(join(FRONTEND_ROOT, 'microfrontends', zone, 'src'))) {
				const body = readFileSync(file, 'utf8');
				if (!callsIt(body)) continue;
				if (body.includes(MARKER)) continue;
				offenders.push(file.slice(file.indexOf('/frontends/') + 1));
			}
		}
		expect(offenders).toEqual([]);
	});

	it('the marker is not free — it has to be followed by something', () => {
		// Guards the gate itself: `POLL REASON:` with nothing after it would satisfy the check above while
		// explaining nothing, which is the failure mode of every marker convention.
		const empty: string[] = [];
		for (const zone of zoneDirs()) {
			for (const file of sources(join(FRONTEND_ROOT, 'microfrontends', zone, 'src'))) {
				const body = readFileSync(file, 'utf8');
				const at = body.indexOf(MARKER);
				if (at < 0) continue;
				const stated = body.slice(at + MARKER.length, at + MARKER.length + 200).trim();
				if (stated.length < 40) empty.push(file.slice(file.indexOf('/frontends/') + 1));
			}
		}
		expect(empty).toEqual([]);
	});
});
