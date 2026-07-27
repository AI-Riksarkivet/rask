import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
	runJobLabel,
	runNotificationId,
	runPhase,
	runPhaseLabel,
	runProgress,
	unreadRuns,
	visibleRuns,
	type RunStatusLike,
	seenOnClose,
} from '../src/lib/runs/run-status.js';

// The run shape and the rules the notification surface is built on. The rendering itself is asserted
// in notifications.test.ts; these are the decisions that must hold before anything is drawn.

const run = (over: Partial<RunStatusLike> & { run_id: string }): RunStatusLike => ({
	job: 'lance-medallion/ingest_events',
	author: 'alice',
	state: 'COMPLETE',
	outputs: ['bronze$events'],
	updated_at: '2026-07-26T13:50:31.632953+00:00',
	...over,
});

describe('the run shape tracks the lineage service, not a hand-written guess', () => {
	// The one thing a structural type cannot enforce for itself: that its field NAMES still match the
	// backend. `RunStatusLike` is spelled out in TypeScript because @repo/ui must not depend on the
	// app API package, which means a Pydantic rename (`error_message` → `error`) would compile fine
	// here and render blanks in every zone. So the field list is compared against the committed
	// contract that `make openapi` dumps — the same file `bun run gen:types` generates from.
	it('declares exactly the fields RunStatus serves', () => {
		const spec = JSON.parse(
			readFileSync(new URL('../../../../docs/lineage-openapi.json', import.meta.url), 'utf8'),
		) as { components: { schemas: { RunStatus: { properties: Record<string, unknown> } } } };
		const served = Object.keys(spec.components.schemas.RunStatus.properties).toSorted();

		const source = readFileSync(new URL('../src/lib/runs/run-status.ts', import.meta.url), 'utf8');
		const body = source.split('export type RunStatusLike = {')[1]?.split('\n};')[0] ?? '';
		const declared = [...body.matchAll(/^\t(\w+)\??:/gm)].map((m) => m[1]).toSorted();

		expect(declared).toEqual(served);
	});
});

describe('runPhase', () => {
	it('reads the OpenLineage lifecycle the fold actually emits', () => {
		expect(runPhase(run({ run_id: 'a', state: 'START' }))).toBe('started');
		expect(runPhase(run({ run_id: 'a', state: 'RUNNING' }))).toBe('running');
		expect(runPhase(run({ run_id: 'a', state: 'COMPLETE' }))).toBe('complete');
		expect(runPhase(run({ run_id: 'a', state: 'FAIL' }))).toBe('failed');
		expect(runPhase(run({ run_id: 'a', state: 'ABORT' }))).toBe('failed');
	});

	it('does not guess at a state it does not know, and says so verbatim', () => {
		expect(runPhase(run({ run_id: 'a', state: 'OTHER' }))).toBe('unknown');
		expect(runPhaseLabel(run({ run_id: 'a', state: 'OTHER' }))).toBe('OTHER');
		expect(runPhase(run({ run_id: 'a', state: null }))).toBe('unknown');
		expect(runPhaseLabel(run({ run_id: 'a', state: null }))).toBe('—');
	});

	it('distinguishes an abort from a plain failure while treating both as failed', () => {
		expect(runPhaseLabel(run({ run_id: 'a', state: 'ABORT' }))).toBe('Aborted');
		expect(runPhaseLabel(run({ run_id: 'a', state: 'FAIL' }))).toBe('Failed');
	});
});

describe('runNotificationId', () => {
	// The rule that makes this a lifecycle feed rather than a list of rows: the notification is the
	// run REACHING a state, so finishing re-rings a bell that starting already rang.
	it('changes when the run changes state, so a completed run is news again', () => {
		const started = run({ run_id: 'r1', state: 'START' });
		const done = run({ run_id: 'r1', state: 'COMPLETE' });
		expect(runNotificationId(started)).toBe('r1@START');
		expect(runNotificationId(done)).toBe('r1@COMPLETE');
		expect(unreadRuns([done], [runNotificationId(started)])).toHaveLength(1);
	});

	it('does NOT change on a progress tick, so a running job cannot spam the bell', () => {
		const first = run({ run_id: 'r1', state: 'RUNNING', progress_done: 1, progress_total: 5 });
		const later = run({ run_id: 'r1', state: 'RUNNING', progress_done: 4, progress_total: 5 });
		expect(runNotificationId(later)).toBe(runNotificationId(first));
		expect(unreadRuns([later], [runNotificationId(first)])).toHaveLength(0);
	});
});

describe('runProgress', () => {
	it('is null without a total — an un-instrumented run must not be drawn as 0%', () => {
		expect(runProgress(run({ run_id: 'a', progress_done: null, progress_total: null }))).toBeNull();
		expect(runProgress(run({ run_id: 'a', progress_total: 0 }))).toBeNull();
	});

	it('reports done/total and a rounded percent', () => {
		expect(runProgress(run({ run_id: 'a', progress_done: 2, progress_total: 5 }))).toEqual({
			done: 2,
			total: 5,
			percent: 40,
		});
	});

	it('clamps a done that outran its total instead of rendering 140%', () => {
		expect(runProgress(run({ run_id: 'a', progress_done: 7, progress_total: 5 }))).toEqual({
			done: 5,
			total: 5,
			percent: 100,
		});
	});
});

describe('runJobLabel', () => {
	it('splits the producer namespace off the job name', () => {
		expect(runJobLabel(run({ run_id: 'a' }))).toEqual({
			name: 'ingest_events',
			namespace: 'lance-medallion',
		});
		expect(runJobLabel(run({ run_id: 'a', job: 'ray-jobs/train' }))).toEqual({
			name: 'train',
			namespace: 'ray-jobs',
		});
	});

	it('falls back to a short run id, because a nameless row is not a notification', () => {
		expect(runJobLabel({ run_id: '415b71fc-14f5-5f7c-a0ea-cca607dddb1f', job: null }).name).toBe(
			'415b71fc',
		);
	});
});

describe('visibleRuns / unreadRuns', () => {
	const runs = [
		run({ run_id: 'old', state: 'COMPLETE', updated_at: '2026-07-26T10:00:00+00:00' }),
		run({ run_id: 'new', state: 'FAIL', updated_at: '2026-07-26T12:00:00+00:00' }),
	];

	it('orders newest first whatever order the caller merged them in', () => {
		expect(visibleRuns(runs).map((r) => r.run_id)).toEqual(['new', 'old']);
	});

	it('drops a dismissed notification from both the list and the count', () => {
		expect(visibleRuns(runs, ['new@FAIL']).map((r) => r.run_id)).toEqual(['old']);
		expect(unreadRuns(runs, [], ['new@FAIL']).map((r) => r.run_id)).toEqual(['old']);
	});

	it('counts only what the viewer has not read', () => {
		expect(unreadRuns(runs, ['old@COMPLETE']).map((r) => r.run_id)).toEqual(['new']);
		expect(unreadRuns(runs, ['old@COMPLETE', 'new@FAIL'])).toHaveLength(0);
	});
});

describe('seenOnClose', () => {
	const run = (id: string, state: string): RunStatusLike =>
		({
			run_id: id,
			state,
			job: id,
			updated_at: `2026-07-26T00:00:${id.padStart(2, '0')}Z`,
		}) as RunStatusLike;

	it('marks only the rows the panel rendered, never the ones it said were not shown', () => {
		// 13 runs, a cap of 8, and the failure pushed past it — the exact shape proven broken in a browser:
		// the badge cleared, `seen` recorded all 13, and the failed run the user never saw was gone.
		const runs = [...Array(12)].map((_, i) => run(String(i), 'COMPLETE'));
		runs.push(run('12', 'FAIL'));

		const ids = seenOnClose(runs, 8);

		expect(ids).toHaveLength(8);
		expect(ids).not.toContain(runNotificationId(runs[12]!)); // the FAIL, past the cap
		expect(new Set(ids).size).toBe(8);
	});

	it('a limit larger than the list marks everything, and a zero limit marks nothing', () => {
		const runs = [run('1', 'COMPLETE'), run('2', 'FAIL')];
		expect(seenOnClose(runs, 99)).toHaveLength(2);
		expect(seenOnClose(runs, 0)).toEqual([]);
	});
});

describe('a failure cannot be buried by recency', () => {
	const at = (n: number) => `2026-07-26T00:00:${String(n).padStart(2, '0')}Z`;
	const run = (id: string, state: string, when: number): RunStatusLike =>
		({ run_id: id, state, job: id, updated_at: at(when) }) as RunStatusLike;

	it('surfaces an OLD failure ahead of newer successes', () => {
		// The live shape: against 891 real runs the first FAIL sat at position 445, so a panel showing the
		// newest 8 displayed no failure at all. Every fixture until now had the failure near the top, which
		// is why the ordering looked correct.
		const runs = [run('old-fail', 'FAIL', 1)];
		for (let i = 2; i <= 40; i++) runs.push(run(`ok-${i}`, 'COMPLETE', i));

		const shown = visibleRuns(runs).slice(0, 8);

		expect(shown[0]?.run_id).toBe('old-fail');
		expect(shown).toHaveLength(8);
	});

	it('within a group the order is still newest first', () => {
		const runs = [
			run('ok-early', 'COMPLETE', 1),
			run('fail-early', 'FAIL', 2),
			run('ok-late', 'COMPLETE', 9),
			run('fail-late', 'FAIL', 8),
		];

		expect(visibleRuns(runs).map((r) => r.run_id)).toEqual([
			'fail-late',
			'fail-early',
			'ok-late',
			'ok-early',
		]);
	});

	it('a DISMISSED failure stays gone — the rule is about unhandled ones', () => {
		const runs = [run('old-fail', 'FAIL', 1), run('ok', 'COMPLETE', 9)];
		const ids = visibleRuns(runs, [runNotificationId(runs[0]!)]).map((r) => r.run_id);
		expect(ids).toEqual(['ok']);
	});
});
