import { test, expect, type Route } from '@playwright/test';
import { STORES } from '../admin/store-fixtures';

// Hermetic coverage for the R18 storage area: /lakehouse/catalog/storage is the S3 object browser over
// the estate's registered STORES, served through this zone's /api/explorer BFF route onto the rask gateway,
// whose /api/explorer row routes to the media-plane viewer's objects endpoints (volumes-api retired in the
// R6/R20 wave). Only the browser's backend calls are stubbed — empty, populated (prefix navigation
// + the text preview pane) and unreachable states are each asserted, so a dead viewer can never
// render as a stuck spinner.
//
// TWO transports, and the split is why the bucket identity below is not ours to choose:
//   · the OBJECT reads (`/api/explorer/*`) are browser-side, so `page.route` still stands in for them;
//   · WHICH stores exist is `listStores()` in src/lib/storage/remote/storage.remote.ts — a remote
//     function that reads the catalog SERVER-SIDE (`CATALOG_API` → e2e/admin/mock-catalog.ts →
//     the STORES fixture). `page.route` cannot reach it, so the browser lands on the registry's
//     FIRST store and the spec asserts against that same fixture rather than a literal.

/** The store the browser lands on. R28 (e0edd88) retired the hardcoded two-value `BUCKETS` union
 *  ('images-batch'/'images-batch-alto') for the catalog's registry, and ObjectBrowser seeds
 *  `bucket = stores[0].name` — so the identity comes from the catalog fixture, not from this file. */
const STORE = STORES[0]!.name;

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('an empty bucket renders the honest empty state, with search + bucket controls', async ({
	page,
}) => {
	await page.route('**/api/explorer/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/explorer/objects')) {
			// Echo the store the page actually asked for — a stand-in that answered a DIFFERENT bucket
			// than the one requested could never catch the page reading the wrong store.
			const bucket = url.searchParams.get('bucket') ?? STORE;
			return json(route, { bucket, prefix: '', prefixes: [], objects: [] });
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
	await page.goto('/lakehouse/catalog/storage');
	await expect(
		page.getByText('No objects under this prefix — the bucket is empty here.'),
	).toBeVisible();
	// The picker names STORES, not buckets (R28) — a store is a NAME the catalog resolves to a bucket,
	// so labelling the control "Bucket" claimed something the value is not. `exact` because this route's
	// layout also ships `<nav aria-label="Storage views">`, which substring-matches "Store".
	await expect(page.getByLabel('Store', { exact: true })).toBeVisible();
	await expect(page.getByPlaceholder('Search objects…')).toBeVisible();
});

test('prefix navigation lists one level and the preview pane decodes a text object', async ({
	page,
}) => {
	await page.route('**/api/explorer/**', (route) => {
		const url = new URL(route.request().url());
		const prefix = url.searchParams.get('prefix') ?? '';
		// Echo the store the page asked for (see the empty-state test) — never a literal of our own.
		const bucket = url.searchParams.get('bucket') ?? STORE;
		if (url.pathname.endsWith('/explorer/objects')) {
			if (prefix === 'vol1/') {
				return json(route, {
					bucket,
					prefix: 'vol1/',
					prefixes: [],
					objects: [
						{ key: 'vol1/readme.txt', size: 24, last_modified: '2026-07-27T10:00:00+00:00' },
					],
				});
			}
			return json(route, { bucket, prefix: '', prefixes: ['vol1/'], objects: [] });
		}
		if (url.pathname.endsWith('/explorer/object')) {
			return json(route, {
				key: 'vol1/readme.txt',
				size: 24,
				content_type: 'text/plain',
				last_modified: '2026-07-27T10:00:00+00:00',
				etag: 'abc123',
			});
		}
		if (url.pathname.endsWith('/explorer/object/download')) {
			return route.fulfill({
				status: 200,
				contentType: 'text/plain',
				body: 'hello from the warehouse',
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
	await page.goto('/lakehouse/catalog/storage');
	// the root level lists the volume "folder"; clicking it descends one delimiter level
	await page.getByRole('button', { name: 'vol1/' }).click();
	const objectRow = page.getByRole('button', { name: /readme\.txt/ });
	await expect(objectRow).toBeVisible();
	// the breadcrumb reflects the prefix
	await expect(page.getByRole('navigation', { name: 'Prefix breadcrumb' })).toContainText('vol1');
	// selecting the object opens the preview pane: metadata + decoded text + the download link
	await objectRow.click();
	const pane = page.getByRole('complementary', { name: 'Object preview' });
	await expect(pane).toContainText('text/plain');
	await expect(pane).toContainText('24 B');
	await expect(pane).toContainText('hello from the warehouse');
	// The `bucket` query param carries a STORE NAME (R28), which the viewer resolves to the real S3
	// bucket through the registry (`_registered_bucket`, services/viewer/.../endpoints/objects.py) —
	// 'wh' → 'rask-wh' here. The name is whatever `listStores()` put first, so this pins the rule
	// (the download link addresses the SELECTED store) instead of the retired `BUCKETS[0]` literal.
	await expect(pane.getByRole('link', { name: 'Download' })).toHaveAttribute(
		'href',
		`/lakehouse/api/explorer/object/download?bucket=${STORE}&key=vol1%2Freadme.txt`,
	);
});

test('a dead storage backend renders the unreachable state with retry — no spinner hang', async ({
	page,
}) => {
	await page.route('**/api/explorer/**', (route) => json(route, { error: 'ECONNREFUSED' }, 502));
	await page.goto('/lakehouse/catalog/storage');
	await expect(page.getByText('Storage service unreachable (HTTP 502).')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
});

test('an unprovisioned bucket names itself instead of claiming the service is unreachable', async ({
	page,
}) => {
	// live-proof 2026-07-28 defect 2. A missing bucket used to raise an unhandled botocore
	// NoSuchBucket in the viewer -> HTTP 500 -> this page rendered "Storage service unreachable",
	// which is wrong about the layer, says nothing about which bucket, and points at no fix. The
	// backend now answers a 404 whose problem+json `detail` carries all three; the browser must show
	// THAT, not paper over it with its own generic outage copy.
	await page.route('**/api/explorer/**', (route) =>
		json(
			route,
			{
				type: 'about:blank#notfounderror',
				title: 'Not Found',
				status: 404,
				detail:
					'bucket not found: images-batch — the S3 backend has no such bucket. The platform ' +
					'provisions it from the chart’s rustfs.buckets; check that the object store ' +
					'actually created it.',
			},
			404,
		),
	);
	await page.goto('/lakehouse/catalog/storage');
	await expect(page.getByText(/bucket not found: images-batch/)).toBeVisible();
	await expect(page.getByText(/rustfs\.buckets/)).toBeVisible();
	await expect(page.getByText('Storage service unreachable (HTTP 404).')).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
});
