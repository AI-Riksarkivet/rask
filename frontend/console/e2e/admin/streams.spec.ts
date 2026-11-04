import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';

// `/admin/streams` is kept (the owner's decision, XC-192) though no route serves the NATS monitor to the
// console: the page names the backend it needs and where the bus is read instead, rather than rendering
// an empty fabric that would read as "no streams".

test('the streams page names the missing NATS monitor instead of rendering an empty fabric', async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.admin });
	await mockMe(page);
	await page.goto('/admin/streams');
	await expect(page.getByText(/needs the NATS monitor/)).toBeVisible();
	await expect(page.getByText(/nats/i).first()).toBeVisible();
});
