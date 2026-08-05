/**
 * The DOCS must name the same zones the manifests do.
 *
 * `manifest.test.ts` already pins the five CODE places a zone must appear. Nothing pinned the two
 * places every agent and every new engineer actually READS first — `CLAUDE.md` and
 * `.claude/skills/rask-frontend/SKILL.md` — and they drifted.
 *
 * The measured damage: `train` was replaced by `models` (same directory slot, same port 5178), and
 * both documents kept describing `train` as a live zone while never mentioning `models` at all. That
 * is not a cosmetic staleness. Routed by that table, an estate-wide audit this session listed seven
 * zones and silently omitted `models` — which happened to be one of the three zones holding a
 * duplicated `tick.svelte.ts`, so the count in the resulting report was simply wrong.
 *
 * A doc gate is unusual and is justified here by the same argument the rest of this package rests
 * on: the estate's SHAPE is asserted, not remembered. A zone roster is shape.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const REPO = join(import.meta.dirname, '../../../..');

/** The zones that actually exist — the directories git tracks under `microfrontends/`. */
function trackedZones(): string[] {
	const out = execFileSync('git', ['ls-files', 'frontend/microfrontends'], {
		cwd: REPO,
		encoding: 'utf8',
		maxBuffer: 32 * 1024 * 1024,
	});
	const zones = new Set<string>();
	for (const line of out.split('\n')) {
		const parts = line.split('/');
		if (parts.length > 2 && parts[2]) zones.add(parts[2]);
	}
	return [...zones].sort();
}

const read = (rel: string) => readFileSync(join(REPO, rel), 'utf8');

/** Which of the real zones a document mentions as a ZONE — matched on its base path, which is how
 *  both documents write them (`/models`, `/annotator`, …). `home` is the catch-all with base `''`
 *  and is named in prose instead, so it is matched by name. */
function zonesNamedIn(text: string, zones: string[]): string[] {
	return zones.filter((z) => (z === 'home' ? /`home`/.test(text) : text.includes(`\`/${z}\``)));
}

describe('the zone roster the DOCS teach', () => {
	it('git tracks exactly the zones the Makefile builds', () => {
		// The anchor: if these two disagree, every assertion below is measuring the wrong set.
		const makefile = read('Makefile');
		const line = makefile.split('\n').find((l) => l.startsWith('ZONES'));
		const declared = (line ?? '').split('=').slice(1).join('=').trim().split(/\s+/).sort();

		expect(declared).toEqual(trackedZones());
	});

	it('CLAUDE.md names every zone that exists, and none that do not', () => {
		// It said `train` (deleted) and never said `models` (real). An agent reading it looked for a
		// zone with no files and did not know about one with eight pages.
		const zones = trackedZones();
		const named = zonesNamedIn(read('CLAUDE.md'), zones);

		expect(named).toEqual(zones);
	});

	it('the rask-frontend skill names every zone that exists', () => {
		const zones = trackedZones();
		const named = zonesNamedIn(read('.claude/skills/rask-frontend/SKILL.md'), zones);

		expect(named).toEqual(zones);
	});

	it('kind-browse.sh forwards exactly the zones that exist', () => {
		// The THIRD place the roster drifted, after CLAUDE.md and the skill. This script carried
		// `media` and `train` — neither has a single tracked file — and omitted `explorer` and
		// `models`, which are real. So browsing the cluster port-forwarded two services that do not
		// exist and offered no route to two that do. A script is not prose: it FAILS at runtime, on
		// someone's machine, while looking like a config typo.
		const text = read('scripts/kind-browse.sh');
		const line = text.split('\n').find((l) => l.includes('[home]=')) ?? '';
		const named = [...line.matchAll(/\[(\w+)\]=/g)].map((m) => m[1] as string).sort();

		expect(named).toEqual(trackedZones());
	});

	it('kind-browse.sh does NOT bind the dev proxy port', () => {
		// `make dev-frontends` binds :3024 for the Turborepo composition proxy. This script used to
		// default to the same port, so whichever started second either died or was silently adopted —
		// and you could believe you were browsing the CLUSTER while looking at local dev servers.
		const text = read('scripts/kind-browse.sh');

		expect(text).not.toMatch(/PORT:-3024/);
	});

	it('neither document still presents `train` as a zone', () => {
		// `train` survives legitimately as the gateway's `/api/train` row and the `ray_train_job`
		// script, so this asserts the ZONE spellings are gone rather than the word.
		for (const doc of ['CLAUDE.md', '.claude/skills/rask-frontend/SKILL.md']) {
			const text = read(doc);
			expect(text, `${doc} still lists a /train ZONE`).not.toMatch(/\|\s*`train`\s*\|/);
			expect(text, `${doc} still routes frontend-train`).not.toMatch(
				/frontend-<zone>[^)]*\btrain\b/,
			);
		}
	});
});
