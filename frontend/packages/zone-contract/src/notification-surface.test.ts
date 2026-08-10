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
 * The bell that actually REMEMBERS — the persistence seam, wired end to end, in EVERY zone.
 *
 * The two gates above pin that the surface is mounted and fed. Neither can see the defect this one
 * exists for: `seen`/`dismissed` are bindable props with `onseen`/`ondismiss` documented as "the
 * persistence seam" (`notification-center.svelte`), and for as long as nothing bound them the read
 * state lived in ONE TAB. Close the tab and a FAILED run you had already dealt with came back
 * unread. A component whose seam has defaults is satisfiable by a zone that never wires it, which is
 * the same shape of "green but wrong" the estate-wide gate above was written for.
 *
 * S1 gated ONE named zone (`home`) on purpose, so that "only home is wired" was a statement this
 * file made out loud rather than a silence. S3 wired the other six and this is the loop that
 * anticipated sentence promised — the constant is gone, and a new zone joins the gate by existing.
 *
 * S3 also added the half no prop-name check can reach: the panel must render INBOX rows. A zone can
 * satisfy every assertion below while still handing the bell nothing but `runs`, and that is exactly
 * the S1 state — a badge counting other people's work and a read mark with no pointer to write to.
 */
describe("the bell's persistence seam is wired in every zone", () => {
	it('the shared transport is a real module the package actually exports', () => {
		// Asserted once rather than per zone, and separately from any zone's import of it: a string
		// match on `@rask/api/inbox` is satisfied by a module that no longer exists and by a package
		// that never published the subpath — and an unpublished subpath fails at BUILD time, not here.
		expect(existsSync(join(FRONTEND_ROOT, 'packages/api/src/inbox.ts'))).toBe(true);
		const pkg = JSON.parse(
			readFileSync(join(FRONTEND_ROOT, 'packages/api/package.json'), 'utf8'),
		) as { exports?: Record<string, unknown> };
		expect(
			pkg.exports?.['./inbox'],
			'@rask/api does not export ./inbox, so no zone can import the transport',
		).toEqual({ types: './src/inbox.ts', default: './src/inbox.ts' });
	});

	for (const zone of zoneDirs()) {
		const dir = join(FRONTEND_ROOT, 'microfrontends', zone);
		const layout = join(dir, 'src/routes/+layout.svelte');
		const inbox = join(dir, 'src/lib/live/inbox.remote.ts');

		it(`${zone} has an inbox transport over the SHARED client, not a hand-rolled fetch ladder`, () => {
			expect(
				existsSync(inbox),
				`${zone} has no src/lib/live/inbox.remote.ts, so its bell cannot persist anything`,
			).toBe(true);
			const source = readFileSync(inbox, 'utf8');
			// `@rask/api/inbox` parses the wire and produces the estate's three-outcome `ApiResult`; a
			// local copy of that ladder is the drift class `@rask/api/upstream` exists to end.
			expect(source).toContain('@rask/api/inbox');
			expect(source).toContain('readInbox');
			expect(source).toContain('markInboxSeen');
			expect(source).toContain('dismissNotification');
			// Remote functions, because the credential is server-side only: the inbox is addressed by
			// the token sub, and the bearer is in an httpOnly cookie the browser cannot read.
			expect(source).toMatch(/export const readInboxState = query\(/);
			expect(source).toMatch(/export const readInboxFeed = query\(/);
			expect(source).toMatch(/export const markSeen = command\(/);
			expect(source).toMatch(/export const dismiss = command\(/);
		});

		it(`${zone} addresses the gateway ABSOLUTELY, not through its own /api`, () => {
			// Not a style rule. `home` and `lakehouse` proxy `/api` to LANCE_GATEWAY_URL (:8001, the
			// LINEAGE service), so a relative `/api/notifications/*` 404s in dev and survives in prod
			// only because the chart happens to aim both names at one Service. That is a coincidence,
			// not a contract, and it breaks the day either name moves.
			const source = readFileSync(inbox, 'utf8');
			expect(
				source.includes('RASK_GATEWAY_URL'),
				`${zone}'s inbox transport does not read RASK_GATEWAY_URL, so it is addressing the wrong upstream`,
			).toBe(true);
		});

		it(`${zone} binds both callbacks into the feed it hands the shell — not left at their defaults`, () => {
			const source = readFileSync(layout, 'utf8');
			expect(source).toContain('$lib/live/inbox.remote');
			// Either spelling counts: the estate threads ONE `NotificationFeed` object (`onseen:`), and
			// a zone that passes the props straight through (`onseen=`) is wired just as truly.
			for (const seam of ['onseen', 'ondismiss', 'seen', 'dismissed']) {
				expect(
					new RegExp(`\\b${seam}\\s*[:=]`).test(source),
					`${zone}'s layout hands AppShell a notifications feed with no \`${seam}\`, so ` +
						'read state is per-tab again and a dealt-with failure comes back on the next reload',
				).toBe(true);
			}
		});

		it(`${zone} renders INBOX rows, not just the activity projection`, () => {
			// The S3 claim, and the one no prop-name check above can reach. A layout can bind every
			// callback, call every transport function, and still hand the bell `runs` alone — which IS
			// the S1 state: the badge counts every run you may SEE while the inbox holds only what was
			// addressed to you, so a mark on someone else's run has no pointer to write to and is
			// unread again after a reload. Passing `inbox` is what makes the two sets one set.
			const source = readFileSync(layout, 'utf8');
			expect(
				/\binbox\s*:/.test(source),
				`${zone}'s layout never passes \`inbox\` to the bell, so its panel still renders the ` +
					"dataset-governed run feed and its badge still counts other people's work",
			).toBe(true);
			expect(
				/\binboxUnread\s*:/.test(source),
				`${zone} passes inbox rows but no \`inboxUnread\`, so the badge is derived from ONE PAGE ` +
					'and shrinks as the reader pages instead of counting the whole inbox',
			).toBe(true);
		});

		it(`${zone}'s callbacks REACH the transport, and the read state is seeded back`, () => {
			// The assertions above are satisfied by `onseen: () => {}` beside an unused import — a seam
			// bound to a NO-OP, which renders and gates exactly like a wired one. That is the shape of
			// the defect this whole file exists for (`OPEN-WORK.md`: shared, tested, shipped, green, and
			// broken until a live drive), so the wiring is asserted at both ends.
			const source = readFileSync(layout, 'utf8');
			for (const [call, consequence] of [
				['markSeen', 'a closed panel marks nothing, so the badge returns on the next reload'],
				['dismiss', 'a dismissal dies with the tab'],
				[
					'readInboxFeed',
					'the panel never receives inbox rows, so it renders the activity projection alone',
				],
			] as const) {
				expect(
					new RegExp(`\\b${call}\\(`).test(source),
					`${zone}'s layout never calls \`${call}\` from $lib/live/inbox.remote: ${consequence}`,
				).toBe(true);
			}
		});
	}
});
