import { expect, test } from '@playwright/test';
import { MOCK_LINEAGE } from '../ports';

// The zone's read side is now change-driven: a `query.live` generator holds the LINEAGE cursor on the
// server and the panels re-read when it advances, instead of each panel running its own `setInterval`.
//
// Two properties have to hold together, and only both together mean anything:
//
//   1. an idle estate issues NO repeat reads — this is what fails against the old timer, which re-read
//      every 5s whether or not anything had happened;
//   2. a change in the estate DOES reach the screen — this is what fails if the timer is simply deleted.
//
// The panel's own read is still browser-side and still mocked with `page.route`. What is server-side is
// the cursor, so it is driven through the mock lineage service's `__mock/*` endpoints (mock-lineage.ts,
// a Playwright webServer that the auth-off dev server's LINEAGE_API points at).

// Serial: the three tests share ONE mock-lineage instance (module-level `seq` and `runs`), and
// `fullyParallel` is on for this project.
test.describe.configure({ mode: 'serial' });

const control = (path: string, body?: unknown) =>
	fetch(`${MOCK_LINEAGE}/__mock/${path}`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: body ? JSON.stringify(body) : undefined,
	});

const run = (id: string, job: string, state: string) => ({
	run_id: id,
	job,
	state,
	progress_done: null,
	progress_total: null,
	error_message: null,
	started_at: '2026-07-26T12:00:00',
	updated_at: `2026-07-26T12:0${id.length}:00`,
	operation: null,
});

test.beforeEach(async () => {
	await control('reset');
});

test('an idle estate makes the runs board read exactly once — no timer', async ({ page }) => {
	let reads = 0;
	await page.route('**/api/runs', async (route) => {
		reads += 1;
		await route.fulfill({
			json: { runs: [run('r1', 'lance-medallion/ingest_events', 'COMPLETE')] },
		});
	});

	await page.goto('/lakehouse/lineage/runs');
	await expect(page.getByText('lance-medallion/ingest_events')).toBeVisible({ timeout: 15_000 });

	// Long enough to have caught two ticks of the 5s timer this replaced. The cursor is polled on the
	// SERVER throughout — the point is that a poll which found nothing new costs the browser nothing.
	await page.waitForTimeout(13_000);
	expect(reads).toBe(1);
});

test('a lineage event reaches the board without a reload', async ({ page }) => {
	let reads = 0;
	await page.route('**/api/runs', async (route) => {
		reads += 1;
		// The second and later reads see the newer board — as if a run had progressed.
		const runs =
			reads === 1
				? [run('r1', 'lance-medallion/ingest_events', 'COMPLETE')]
				: [
						run('r2', 'ray-jobs/train_classifier', 'RUNNING'),
						run('r1', 'lance-medallion/ingest_events', 'COMPLETE'),
					];
		await route.fulfill({ json: { runs } });
	});

	await page.goto('/lakehouse/lineage/runs');
	await expect(page.getByText('lance-medallion/ingest_events')).toBeVisible({ timeout: 15_000 });
	await expect(page.getByText('ray-jobs/train_classifier')).toBeHidden();

	// Something happened in the estate. Nothing else changes — no click, no navigation, no reload.
	await control('bump');

	await expect(page.getByText('ray-jobs/train_classifier')).toBeVisible({ timeout: 15_000 });
});

test('the shell bell renders runs from the server-trimmed /runs feed', async ({ page }) => {
	// The bell's rows come from the live query — a SERVER-side read of `GET /runs`, trimmed to a window
	// before it crosses the wire — not from a browser fetch. So nothing is mocked client-side here, and
	// the page is deliberately the jobs list, which renders nothing from `/runs`: if this run's name is
	// on screen at all, the bell put it there.
	await control('runs', { runs: [run('r9', 'lance-medallion/silver_to_gold', 'FAIL')] });

	await page.goto('/lakehouse/lineage/jobs');
	const bell = page.getByRole('button', { name: /notifications/i });
	await expect(bell).toBeVisible({ timeout: 15_000 });
	// The count is on the trigger with the panel closed — the point of the surface.
	await expect(bell).toHaveAccessibleName(/1 unread/, { timeout: 15_000 });

	await bell.click();
	const row = page.getByTitle('lance-medallion/silver_to_gold');
	await expect(row).toBeVisible({ timeout: 15_000 });
	await expect(page.getByRole('button', { name: 'Dismiss all' })).toBeVisible();
});
