import { test } from '@playwright/test';
import { E2E, RUNNER_APP } from './ports';

// Compile BOTH app servers' heavy routes once, one at a time, before any spec runs.
//
// Two reasons, and the second is the load-bearing one:
//  · a cold Vite compile of the canvas route costs ~20 s, which the first spec to reach it pays out
//    of its own timeout;
//  · the two dev servers share this project directory, so their first compiles must not overlap.
//    Serialising them here (plus the per-server `cacheDir` in vite.config.ts) is what keeps a COLD
//    run — every CI run — from the dependency-optimizer race that shows up as SSR 500s in whichever
//    specs happen to be running when the other server optimizes.
//
// Absolute URLs on purpose: this one test drives both servers, so it cannot use a project baseURL.

// The LANDING plus the project-detail route, on each server — deliberately not the canvas or
// /browse. Warming those too concentrates every heavy compile in this one test, and a burst that
// large is itself a hazard on a shared box; the specs that need them pay a single lazy compile
// inside their own 60 s budget. The project-detail route earns its warmup slot the same way the
// lakehouse admin routes did: cold it compiles in ~20 s, and under a full-parallel run the first
// projects spec to reach it lost its 5 s assertion window to that compile (the expired-lease flake).
test('both app servers have compiled before the suite', async ({ page }) => {
	for (const url of [
		`${E2E}/annotator/`,
		`${E2E}/annotator/projects/p1`,
		`${RUNNER_APP}/annotator/`,
	]) {
		await page.goto(url);
	}
});
