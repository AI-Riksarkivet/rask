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
