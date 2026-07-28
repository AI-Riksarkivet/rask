import { test, expect, type Route } from '@playwright/test';

// Hermetic coverage for the R18 storage area: /lakehouse/storage is the S3 object browser over the
// two rask buckets, served through this zone's /api/media BFF route onto the rask gateway, whose
// /api/media row routes to the media-plane viewer's objects endpoints (volumes-api retired in the
// R6/R20 wave). Only the browser's backend calls are stubbed — empty, populated (prefix navigation
// + the text preview pane) and unreachable states are each asserted, so a dead viewer can never
// render as a stuck spinner.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('an empty bucket renders the honest empty state, with search + bucket controls', async ({
	page,
}) => {
	await page.route('**/api/media/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/media/objects')) {
			return json(route, { bucket: 'images-batch', prefix: '', prefixes: [], objects: [] });
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
	await page.goto('/lakehouse/storage');
	await expect(
		page.getByText('No objects under this prefix — the bucket is empty here.'),
	).toBeVisible();
	await expect(page.getByLabel('Bucket')).toBeVisible();
	await expect(page.getByPlaceholder('Search objects…')).toBeVisible();
});

test('prefix navigation lists one level and the preview pane decodes a text object', async ({
	page,
}) => {
	await page.route('**/api/media/**', (route) => {
		const url = new URL(route.request().url());
		const prefix = url.searchParams.get('prefix') ?? '';
		if (url.pathname.endsWith('/media/objects')) {
			if (prefix === 'vol1/') {
				return json(route, {
					bucket: 'images-batch',
					prefix: 'vol1/',
					prefixes: [],
					objects: [
						{ key: 'vol1/readme.txt', size: 24, last_modified: '2026-07-27T10:00:00+00:00' },
					],
				});
			}
			return json(route, { bucket: 'images-batch', prefix: '', prefixes: ['vol1/'], objects: [] });
		}
		if (url.pathname.endsWith('/media/object')) {
			return json(route, {
				key: 'vol1/readme.txt',
				size: 24,
				content_type: 'text/plain',
				last_modified: '2026-07-27T10:00:00+00:00',
				etag: 'abc123',
			});
		}
		if (url.pathname.endsWith('/media/object/download')) {
			return route.fulfill({
				status: 200,
				contentType: 'text/plain',
				body: 'hello from the warehouse',
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
	await page.goto('/lakehouse/storage');
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
	await expect(pane.getByRole('link', { name: 'Download' })).toHaveAttribute(
		'href',
		'/lakehouse/api/media/object/download?bucket=images-batch&key=vol1%2Freadme.txt',
	);
});

test('a dead storage backend renders the unreachable state with retry — no spinner hang', async ({
	page,
}) => {
	await page.route('**/api/media/**', (route) => json(route, { error: 'ECONNREFUSED' }, 502));
	await page.goto('/lakehouse/storage');
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
	await page.route('**/api/media/**', (route) =>
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
	await page.goto('/lakehouse/storage');
	await expect(page.getByText(/bucket not found: images-batch/)).toBeVisible();
	await expect(page.getByText(/rustfs\.buckets/)).toBeVisible();
	await expect(page.getByText('Storage service unreachable (HTTP 404).')).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
});
