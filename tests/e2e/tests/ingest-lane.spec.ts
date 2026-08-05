import { expect, test } from '@playwright/test';

// A20 — the UI proves the lane.
//
// These run against a DEPLOYED estate (`RASK_E2E_BASE_URL`), not a mocked zone. That is the whole
// point of the gate: the per-zone Playwright suites are hermetic by design and would happily pass
// against a backend that does not exist. A11 proved the lane end to end at the API; A20 proves the
// same run is visible to a person.
//
// The run id comes from `.a11-run-id`, written by `scripts/ingest-lane.sh run`. Passed as an env var
// rather than re-ingested here: re-running the lane inside a UI test would make a browser test
// responsible for infrastructure, and its failures would stop being about the UI.

const RUN_ID = process.env.RASK_E2E_INGEST_RUN ?? '';
const API = process.env.RASK_E2E_API_URL ?? 'http://localhost:8888';
const ERRORED_RUN = process.env.RASK_E2E_INGEST_ERRORED_RUN ?? '';

test.describe('A20 — the deployed ingest lane, seen from the UI', () => {
	test.skip(
		!RUN_ID,
		'needs a run from the deployed lane: run `scripts/ingest-lane.sh run`, then ' +
			'RASK_E2E_INGEST_RUN=$(cat .a11-run-id) bun --cwd=tests/e2e run test',
	);

	test('the run-status page shows a COMPLETE run with its committed version', async ({ page }) => {
		// The assertion the first in-cluster lane could NOT make: the API answered ACCEPTED for a run
		// the engine had already completed, because the status came from a cache written once at
		// accept time and never updated. Reading it through the UI is what makes that visible to
		// someone who is not tailing pod logs.
		await page.goto(`/compute/ingest/${RUN_ID}`);

		await expect(page.getByTestId('run-id')).toHaveText(RUN_ID);
		await expect(page.getByTestId('run-status')).toContainText('COMPLETE');

		// A committed version means bronze actually took a new version — a run can report success and
		// commit nothing (an empty source does exactly that), so the version is the load-bearing field.
		await expect(page.getByTestId('committed-version')).not.toHaveText('—');
		await expect(page.getByTestId('units-done')).toHaveText(/[1-9]\d*/);
	});

	test('A8 — a run with provenance reports NO defect banner', async ({ page }) => {
		// The negative case is the one worth asserting. `lineage_run_present` defaulted to false and
		// nothing set it, so every completed run wore a provenance defect — and a banner that appears
		// on every run is one an operator learns to scroll past, which is how the single real
		// provenance hole would go unnoticed.
		await page.goto(`/compute/ingest/${RUN_ID}`);

		await expect(page.getByTestId('run-status')).toContainText('COMPLETE');
		await expect(page.getByTestId('run-defect')).toHaveCount(0);
	});

	test('the lineage graph holds a run for this ingest', async ({ request }) => {
		// The chain the UI's defect banner is derived from, asserted directly against the graph so a
		// UI that simply never renders the banner cannot pass the test above by accident.
		const response = await request.get(`${API}/api/lineage/runs`);
		expect(response.ok()).toBeTruthy();

		const body = await response.json();
		expect(Array.isArray(body.runs)).toBeTruthy();
		expect(body.runs.length).toBeGreaterThan(0);
	});

	test('an unknown run says so, instead of loading forever', async ({ page }) => {
		// FOUND BY DRIVING IT. The page shipped one revision where an unknown run id left the query
		// undefined forever, so it rendered "Loading run…" permanently at HTTP 200 while polling every
		// two seconds for a run that would never exist. To an operator that is indistinguishable from
		// a run still working — the single worst thing a status page can get wrong, because it turns a
		// typo into an apparently-live harvest.
		await page.goto('/compute/ingest/does-not-exist-0000');

		await expect(page.getByTestId('run-unavailable')).toContainText('No such run');
		await expect(page.getByText('Loading run')).toHaveCount(0);
	});

	test('the ingest form offers every REGISTERED kind, not just IIIF', async ({ page }) => {
		// I1 at the UI boundary. The form used to call `ingestIIIFVolume()` with kind 'iiif', project
		// 'default' and dataset 'pages' baked in — under its own comment saying the door was
		// source-agnostic. So `S3PrefixSource` was reachable by curl and by nobody using the product,
		// which is the same "written, tested, unreachable" state it sat in for months inside the
		// medallion.
		//
		// Asserted against a DEPLOYED registry rather than a fixture: the point is that the page renders
		// what this deployment actually registered, and a mock would assert only that the mock was read.
		await page.goto('/compute/etl');

		const kinds = page.getByTestId('source-kind');
		await expect(kinds).toBeVisible();

		const offered = await kinds.locator('option').allTextContents();
		expect(offered.length).toBeGreaterThan(1);
		expect(offered.join('|')).toContain('S3 prefix');
	});

	test('choosing a kind renders THAT kind s fields', async ({ page }) => {
		// The half that proves the fields come from the registry rather than from a hardcoded form:
		// switching kinds must change which inputs exist. `bucket` belongs to s3-prefix alone, and
		// `volume_id` to iiif alone, so each is absent under the other.
		await page.goto('/compute/etl');

		await page.getByTestId('source-kind').selectOption('s3-prefix');
		await expect(page.getByTestId('source-option-bucket')).toBeVisible();
		await expect(page.getByTestId('source-option-volume_id')).toHaveCount(0);

		await page.getByTestId('source-kind').selectOption('iiif');
		await expect(page.getByTestId('source-option-volume_id')).toBeVisible();
		await expect(page.getByTestId('source-option-bucket')).toHaveCount(0);
	});

	test('a run with a corrupt unit NAMES the unit that refused to land', async ({ page }) => {
		// A5 seen from the UI. "3 units failed" tells an operator a number; the unit keys tell them
		// which pages to look at, which is the only form of the answer they can act on.
		test.skip(!ERRORED_RUN, 'needs the A5 run: scripts/ingest-lane.sh corrupt');
		await page.goto(`/compute/ingest/${ERRORED_RUN}`);

		await expect(page.getByTestId('run-status')).toContainText('COMPLETE_WITH_ERRORS');
		await expect(page.getByTestId('run-errors')).toContainText('.tif');
	});
});
