import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

/**
 * Every zone mounts the run-notification bell, and every zone has a transport to feed it.
 *
 * `@rask/ui`'s `NotificationCenter` was built shared on purpose — the annotator had already forked the
 * estate header once and drifted — but sharing a component only guarantees that the zones which mount it
 * agree. Measured live, signed in as alice, against the deployed images:
 *
 *     bell in home: 0 · lakehouse: 1 · media: 0 · annotator: 0
 *
 * One zone out of four, and the wrong one: a person waiting on a batch is in the annotator or the media
 * zone doing the work that batch feeds, not on the run board where they would have to go to learn it
 * failed. Nothing failed, no test went red, and the component's own docstring said every zone got it.
 *
 * That is the exact shape of claim this package exists to gate, so this is the gate. Both halves are
 * required, because either alone is satisfiable by a broken zone: a mounted bell with no feed renders an
 * empty panel forever, and a feed nobody mounts is dead code.
 */
describe('the notification surface is estate-wide, not lakehouse-only', () => {
	for (const zone of zoneDirs()) {
		const dir = join(FRONTEND_ROOT, 'microfrontends', zone);
		const layout = join(dir, 'src/routes/+layout.svelte');
		const feed = join(dir, 'src/lib/live/feeds.remote.ts');

		it(`${zone} passes {notifications} to AppShell`, () => {
			const source = readFileSync(layout, 'utf8');
			expect(source, `${zone}'s root layout does not mount the estate shell`).toContain('AppShell');
			expect(
				/\{notifications\}|notifications=\{/.test(source),
				`${zone}'s root layout renders AppShell WITHOUT a notifications feed, so a run that ` +
					'fails is invisible to anyone working in this zone',
			).toBe(true);
		});

		it(`${zone} has a lineage feed to fill it`, () => {
			expect(existsSync(feed), `${zone} has no src/lib/live/feeds.remote.ts`).toBe(true);
			const source = readFileSync(feed, 'utf8');
			// The SHARED generator, not a fourth copy of the probe/trim/keepalive logic.
			expect(source).toContain('@rask/api/runs-feed');
			expect(source).toContain('lineagePulse');
			expect(source).toMatch(/export const lineageFeed = query\.live/);
		});
	}
});

/**
 * The zone whose bell actually REMEMBERS — the persistence seam, wired end to end.
 *
 * The two gates above pin that the surface is mounted and fed. Neither can see the defect this one
 * exists for: `seen`/`dismissed` are bindable props with `onseen`/`ondismiss` documented as "the
 * persistence seam" (`notification-center.svelte:44-48`), and for as long as nothing bound them the
 * read state lived in ONE TAB. Close the tab and a FAILED run you had already dealt with came back
 * unread. A component whose seam has defaults is satisfiable by a zone that never wires it, which
 * is the same shape of "green but wrong" the estate-wide gate above was written for.
 *
 * ONE ZONE, deliberately (S1 in `open_notifications.md` §9; the estate-wide sweep is S3), and it is
 * asserted as a NAMED zone rather than a loop so that "only home is wired" is a statement this file
 * makes out loud rather than a silence. S3 turns the constant into `zoneDirs()` and deletes this
 * sentence.
 *
 * Home is the one that can be wired first: the inbox is subject-derived from the verified token's
 * `sub`, the bearer lives in the sealed session cookie a zone's BFF holds, and home owns the OIDC
 * BFF and the catch-all base — so it is the zone every signed-in user passes through and the only
 * one with a signed-in Playwright fixture to prove the seam across a fresh browser context.
 */
const S1_INBOX_ZONE = 'home';

describe(`the bell's persistence seam is wired in ${S1_INBOX_ZONE}`, () => {
	const dir = join(FRONTEND_ROOT, 'microfrontends', S1_INBOX_ZONE);
	const layout = join(dir, 'src/routes/+layout.svelte');
	const inbox = join(dir, 'src/lib/live/inbox.remote.ts');

	it('the zone still exists under that name', () => {
		// The constant is a name, and a renamed zone must fail here rather than skip silently.
		expect(zoneDirs()).toContain(S1_INBOX_ZONE);
	});

	it('the shared transport is a real module the package actually exports', () => {
		// Asserted separately from the zone's import of it, because a string match on `@rask/api/inbox`
		// is satisfied by a module that no longer exists and by a package that never published the
		// subpath — and an unpublished subpath is the failure that only shows up at BUILD time, in the
		// six zones S3 wires next rather than in this one.
		expect(existsSync(join(FRONTEND_ROOT, 'packages/api/src/inbox.ts'))).toBe(true);
		const pkg = JSON.parse(
			readFileSync(join(FRONTEND_ROOT, 'packages/api/package.json'), 'utf8'),
		) as { exports?: Record<string, unknown> };
		expect(
			pkg.exports?.['./inbox'],
			'@rask/api does not export ./inbox, so no zone can import the transport',
		).toEqual({ types: './src/inbox.ts', default: './src/inbox.ts' });
	});

	it('has an inbox transport over the SHARED client, not a hand-rolled fetch ladder', () => {
		expect(
			existsSync(inbox),
			`${S1_INBOX_ZONE} has no src/lib/live/inbox.remote.ts, so its bell cannot persist anything`,
		).toBe(true);
		const source = readFileSync(inbox, 'utf8');
		// `@rask/api/inbox` parses the wire and produces the estate's three-outcome `ApiResult`; a
		// seventeenth local copy of that ladder is the drift class `@rask/api/upstream` exists to end.
		expect(source).toContain('@rask/api/inbox');
		expect(source).toContain('readInbox');
		expect(source).toContain('markInboxSeen');
		expect(source).toContain('dismissNotification');
		// Remote functions, because the credential is server-side only: the inbox is addressed by the
		// token sub, and the bearer is in an httpOnly cookie the browser cannot read.
		expect(source).toMatch(/export const readInboxState = query\(/);
		expect(source).toMatch(/export const markSeen = command\(/);
		expect(source).toMatch(/export const dismiss = command\(/);
	});

	it('binds both callbacks into the feed it hands the shell — not left at their defaults', () => {
		const source = readFileSync(layout, 'utf8');
		expect(source).toContain('$lib/live/inbox.remote');
		// Either spelling counts: the estate threads ONE `NotificationFeed` object (`onseen:`), and a
		// zone that ever passes the props straight through (`onseen=`) is wired just as truly. What is
		// gated is that the seam is connected, not how the zone spells it.
		for (const seam of ['onseen', 'ondismiss', 'seen', 'dismissed']) {
			expect(
				new RegExp(`\\b${seam}\\s*[:=]`).test(source),
				`${S1_INBOX_ZONE}'s layout hands AppShell a notifications feed with no \`${seam}\`, so ` +
					'read state is per-tab again and a dealt-with failure comes back on the next reload',
			).toBe(true);
		}
	});

	it('the callbacks REACH the transport, and the read state is seeded back', () => {
		// The assertions above are satisfied by `onseen: () => {}` beside an unused import — a seam
		// bound to a NO-OP, which renders and gates exactly like a wired one and leaves read state per
		// tab. That is the shape of the defect this whole file exists for (`OPEN-WORK.md:1563-1569`:
		// shared, tested, shipped, green, and broken until a live drive), so the wiring is asserted at
		// both ends rather than at the prop names alone.
		const source = readFileSync(layout, 'utf8');
		for (const [call, consequence] of [
			['markSeen', 'a closed panel marks nothing, so the badge returns on the next reload'],
			['dismiss', 'a dismissal dies with the tab'],
			[
				'readInboxState',
				'nothing is ever read BACK, so a fresh browser context shows every row unread — the B2 acceptance itself',
			],
		] as const) {
			expect(
				new RegExp(`\\b${call}\\(`).test(source),
				`${S1_INBOX_ZONE}'s layout never calls \`${call}\` from $lib/live/inbox.remote: ${consequence}`,
			).toBe(true);
		}
	});
});
