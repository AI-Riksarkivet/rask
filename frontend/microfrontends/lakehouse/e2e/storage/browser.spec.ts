import { test, expect, type Route } from '@playwright/test';

// Hermetic coverage for the R18 storage area: /lakehouse/storage is the S3 object browser over the
// two rask buckets, served through this zone's /api/v1/volumes BFF route onto the rask gateway.
// Only the browser's backend calls are stubbed — empty, populated (prefix navigation + the text
// preview pane) and unreachable states are each asserted, so a dead volumes-api can never render as
// a stuck spinner.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('an empty bucket renders the honest empty state, with search + bucket controls', async ({
	page,
}) => {
	await page.route('**/api/v1/volumes/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/volumes/objects')) {
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
	await page.route('**/api/v1/volumes/**', (route) => {
		const url = new URL(route.request().url());
		const prefix = url.searchParams.get('prefix') ?? '';
		if (url.pathname.endsWith('/volumes/objects')) {
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
		if (url.pathname.endsWith('/volumes/object')) {
			return json(route, {
				key: 'vol1/readme.txt',
				size: 24,
				content_type: 'text/plain',
				last_modified: '2026-07-27T10:00:00+00:00',
				etag: 'abc123',
			});
		}
		if (url.pathname.endsWith('/volumes/object/download')) {
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
		'/lakehouse/api/v1/volumes/object/download?bucket=images-batch&key=vol1%2Freadme.txt',
	);
});

test('a dead volumes service renders the unreachable state with retry — no spinner hang', async ({
	page,
}) => {
	await page.route('**/api/v1/volumes/**', (route) => json(route, { error: 'ECONNREFUSED' }, 502));
	await page.goto('/lakehouse/storage');
	await expect(page.getByText('Volumes service unreachable (HTTP 502).')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
});
