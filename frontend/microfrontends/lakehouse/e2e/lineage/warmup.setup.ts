import { test } from '@playwright/test';

// Compile-warm the dev server before the parallel suite (see playwright.config.ts projects comment).
// No mocks here — failed /api calls are irrelevant; goto resolving means Vite compiled the route.
test('warm the dev server routes', async ({ page }) => {
	test.setTimeout(180_000);
	for (const path of [
		'/lakehouse/lineage',
		'/lakehouse/lineage/datasets',
		'/lakehouse/lineage/datasets/silver%24features',
		'/lakehouse/lineage/jobs',
		'/lakehouse/lineage/jobs/medallion.silver',
		'/lakehouse/lineage/runs',
		'/lakehouse/lineage/columns',
		// The WORKBENCH is the heaviest route in this zone — it dynamically imports @rask/dockview
		// (~100 KB gz) on top of the lineage graph's @xyflow/svelte. Omitting it is what turned
		// `workbench.spec.ts` red in CI while it passed locally: a GitHub runner has 2-4 cores, so the
		// first spec to reach a cold route starves behind the compile and blows the 30s timeout. Every
		// other spec here is warmed; the one with the most to compile was the one missing.
		'/lakehouse/lineage/workbench',
	]) {
		await page.goto(path).catch(() => {});
	}
});
