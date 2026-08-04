import { test } from '@playwright/test';
import { E2E } from './ports';

// Compile the app server's heavy routes once, before any spec runs: a cold Vite compile of the
// canvas route costs ~20 s, which the first spec to reach it would otherwise pay out of its own
// timeout.
//
// This used to serialise TWO dev servers, whose shared project directory made their first compiles
// race in the dependency optimizer (SSR 500s in whichever specs happened to be running). The second
// server existed only to set `MEDIA_ASSIST_URL`; runner presence now comes from the service, so both
// the server and that race are gone.

// The LANDING plus the project-detail route, on each server — deliberately not the canvas or
// /browse. Warming those too concentrates every heavy compile in this one test, and a burst that
// large is itself a hazard on a shared box; the specs that need them pay a single lazy compile
// inside their own 60 s budget. The project-detail route earns its warmup slot the same way the
// lakehouse admin routes did: cold it compiles in ~20 s, and under a full-parallel run the first
// projects spec to reach it lost its 5 s assertion window to that compile (the expired-lease flake).
test('the app server has compiled before the suite', async ({ page }) => {
	for (const url of [`${E2E}/annotator/`, `${E2E}/annotator/projects/p1`]) {
		await page.goto(url);
	}
});
