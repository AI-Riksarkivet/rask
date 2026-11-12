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
	// innerText: the notice reads as ONE sentence, not as text runs laid out side by side as columns.
	await expect(page.getByText(/needs the NATS monitor/)).toHaveText(
		"The stream view needs the NATS monitor (/jsz), which no route serves to the console. Read the bus with NATS's own tools: the nats CLI, its Prometheus exporter, or Surveyor.",
		{ useInnerText: true },
	);
	// A read that can never be served offers no Refresh, and the page claims no source it does not read.
	await expect(page.getByRole('button', { name: 'Refresh' })).toHaveCount(0);
	await expect(page.locator('header .sub')).toHaveText(
		"JetStream streams and their consumers' lag",
	);
});
