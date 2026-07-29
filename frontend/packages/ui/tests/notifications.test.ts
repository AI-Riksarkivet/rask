import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import NotificationCenter from '../src/lib/shell/notification-center.svelte';
import NotificationList from '../src/lib/shell/notification-list.svelte';
import TopNavbar from '../src/lib/shell/top-navbar.svelte';
import type { RunStatusLike } from '../src/lib/runs/run-status.js';

// What a user SEES, asserted on rendered markup rather than on props — a prop assertion passes on a
// component that renders nothing at all.
//
// These render on the SERVER (`svelte/server`), which is exactly how a zone first paints them. That
// puts one boundary on this file: the bell's panel is portalled, and a portal emits nothing during
// SSR, so the ROWS are asserted by rendering `NotificationList` (the panel's real markup, the same
// component the centre mounts inside its popover) and the BELL — count, label — by rendering
// `NotificationCenter`. Opening the popover with a pointer is browser work; that path is driven by
// the harness in scripts/ and screenshotted, not simulated here.

/** The visible text of a render — tags and Svelte's SSR comment markers stripped, whitespace
 *  collapsed. This is what a reader of the panel actually reads.
 *
 *  Tags are walked rather than regex-stripped, because a tailwind class list contains `>` (the
 *  badge's `[&>svg]:size-3`): a `<[^>]+>` strip ends the tag early and leaks half the stylesheet
 *  into the "text", which is how the first run of this file failed. */
const text = (html: string) => {
	let out = '';
	let inTag = false;
	let quote = '';
	for (const ch of html.replace(/<!--[\s\S]*?-->/g, '')) {
		if (!inTag) {
			if (ch === '<') {
				inTag = true;
				out += ' ';
			} else out += ch;
		} else if (quote) {
			if (ch === quote) quote = '';
		} else if (ch === '"' || ch === "'") quote = ch;
		else if (ch === '>') inTag = false;
	}
	return out
		.replace(/&amp;/g, '&')
		.replace(/&#39;/g, "'")
		.replace(/&quot;/g, '"')
		.replace(/\s+/g, ' ')
		.trim();
};

const count = (html: string, needle: string) => html.split(needle).length - 1;

const NOW = Date.parse('2026-07-26T13:55:00+00:00');

// Four runs, one per lifecycle state, shaped exactly like the live payload
// (GET /lakehouse/api/runs as alice, 2026-07-26): `job` is `namespace/name`, `state` is the raw
// OpenLineage event type, `outputs` are dataset names, and the medallion runs carry no progress.
const RUNS: RunStatusLike[] = [
	{
		run_id: 'run-start',
		job: 'lance-medallion/ingest_events',
		author: 'alice',
		state: 'START',
		outputs: [],
		updated_at: '2026-07-26T13:50:00+00:00',
		events: 1,
	},
	{
		run_id: 'run-running',
		job: 'ray-jobs/embed_features',
		author: 'ray',
		state: 'RUNNING',
		progress_done: 2,
		progress_total: 5,
		outputs: ['silver$features'],
		updated_at: '2026-07-26T13:49:00+00:00',
		events: 3,
	},
	{
		run_id: 'run-done',
		job: 'lance-medallion/aggregate_gold',
		author: 'analyst',
		state: 'COMPLETE',
		outputs: ['gold$catalog'],
		updated_at: '2026-07-26T13:48:00+00:00',
		events: 4,
	},
	{
		run_id: 'run-failed',
		job: 'lance-medallion/embed_features',
		author: 'data_eng',
		state: 'FAIL',
		error_message: 'ValueError: embedding dim 384 != table dim 768',
		outputs: [],
		updated_at: '2026-07-26T13:47:00+00:00',
		events: 2,
	},
];

const list = (props: Record<string, unknown> = {}) =>
	render(NotificationList, { props: { runs: RUNS, now: NOW, ...props } }).body;

const centre = (props: Record<string, unknown> = {}) =>
	render(NotificationCenter, { props: { runs: RUNS, now: NOW, ...props } }).body;

describe('the panel renders run lifecycle', () => {
	it('shows a started run as Started, naming the job', () => {
		expect(text(list())).toContain('ingest_events Started');
	});

	it('shows a completed run as Completed, with what it wrote', () => {
		const seen = text(list());
		expect(seen).toContain('aggregate_gold Completed');
		expect(seen).toContain('→ gold$catalog');
	});

	it('shows a FAILED run as Failed AND prints its error message', () => {
		const html = list();
		expect(text(html)).toContain('embed_features Failed');
		// The whole point of the surface: the reason is on screen, not one navigation away.
		expect(text(html)).toContain('ValueError: embedding dim 384 != table dim 768');
		expect(html).toContain('data-slot="notification-error"');
	});

	it('shows how far a running run has got, as words and as a progress bar', () => {
		const html = list();
		expect(text(html)).toContain('embed_features Running');
		expect(text(html)).toContain('2 of 5');
		// The bar a sighted user sees and the value a screen reader announces are the same 40%.
		expect(html).toContain('aria-valuenow="40"');
	});

	it('draws NO progress bar for the three runs that never reported one', () => {
		// A 0% bar on an un-instrumented run reads as "stuck at zero". Only the RUNNING row has one.
		expect(count(list(), 'role="progressbar"')).toBe(1);
	});

	it('says when each run last moved, on the clock it is given', () => {
		expect(text(list())).toContain('5m ago');
	});

	it('attributes each run to its author and producer namespace', () => {
		expect(text(list())).toContain('ray-jobs · ray');
		expect(text(list())).toContain('lance-medallion · alice');
	});
});

describe('unread and dismiss', () => {
	it('marks every row unread when nothing has been read', () => {
		expect(count(list(), 'aria-label="Unread"')).toBe(4);
	});

	it('drops the unread mark from exactly the rows already read', () => {
		const html = list({ seen: ['run-done@COMPLETE', 'run-failed@FAIL'] });
		expect(count(html, 'aria-label="Unread"')).toBe(2);
		expect(text(html)).toContain('aggregate_gold Completed'); // still listed, just not new
	});

	it('does not render a dismissed run at all', () => {
		const html = list({ dismissed: ['run-failed@FAIL'] });
		expect(text(html)).not.toContain('ValueError: embedding dim 384 != table dim 768');
		expect(count(html, 'data-slot="notification"')).toBe(3);
	});

	it('offers a per-row dismiss control that names the run it would dismiss', () => {
		expect(list()).toContain('aria-label="Dismiss ingest_events"');
	});

	it('caps the panel and says how many older runs it is not showing', () => {
		expect(text(list({ limit: 2 }))).toContain('2 older runs not shown');
		expect(text(list({ limit: 3 }))).toContain('1 older run not shown');
	});

	it('says so plainly when there is nothing to report', () => {
		expect(text(list({ runs: [] }))).toContain('No run activity yet');
	});
});

describe('the bell', () => {
	it('carries the unread count where it can be read with the panel shut', () => {
		const html = centre();
		expect(html).toContain('aria-label="Notifications, 4 unread"');
		expect(text(html)).toBe('4');
	});

	it('shows no count once every run has been read', () => {
		const seen = RUNS.map((r) => `${r.run_id}@${r.state}`);
		const html = centre({ seen });
		expect(html).toContain('aria-label="Notifications"');
		expect(html).not.toContain('data-slot="notification-count"');
	});

	it('counts a run again when it reaches a NEW state', () => {
		// Read at START; the same run then fails. That is news, and the bell must say so — this is the
		// difference between a lifecycle feed and a list of run ids.
		const runs = [{ ...RUNS[0]!, state: 'FAIL', error_message: 'boom' }];
		expect(centre({ runs, seen: ['run-start@START'] })).toContain(
			'aria-label="Notifications, 1 unread"',
		);
	});

	it('does not count what has been dismissed', () => {
		const html = centre({ dismissed: ['run-failed@FAIL', 'run-done@COMPLETE'] });
		expect(html).toContain('aria-label="Notifications, 2 unread"');
	});

	it('is absent from the navbar until a zone hands the shell a feed', () => {
		// The opt-in half of the contract. Three zones have not wired `/runs` yet, and a bell that is
		// permanently empty (or worse, one their e2e suites now have to know about) is not an
		// improvement — so no feed, no bell, and the account cluster keeps its position.
		const bare = render(TopNavbar, { props: { pathname: '/lakehouse' } }).body;
		expect(bare).not.toContain('Notifications');

		const wired = render(TopNavbar, {
			props: { pathname: '/lakehouse', notifications: { runs: RUNS } },
		}).body;
		expect(wired).toContain('aria-label="Notifications, 4 unread"');
	});

	it('stops counting exactly at 99+', () => {
		const many = Array.from({ length: 150 }, (_, i) => ({
			run_id: `r${i}`,
			job: 'lance-medallion/ingest_events',
			state: 'COMPLETE',
			updated_at: '2026-07-26T13:00:00+00:00',
		}));
		expect(text(centre({ runs: many }))).toBe('99+');
		expect(centre({ runs: many })).toContain('aria-label="Notifications, 150 unread"');
	});
});
