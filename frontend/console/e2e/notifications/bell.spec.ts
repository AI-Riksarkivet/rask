import { test, expect } from '@playwright/test';
import { ME_MEMBER, signIn, TOKEN } from '../session';
import { seed } from '../mock-client';
import { MOCK_INBOX } from './inbox-fixture';

// THE BELL reads the signed-in person's inbox, with their bearer, once the session exists. The inbox
// is subject-derived and the mock refuses a read without a token, so a console that reads before
// sign-in completes (or never reads at all) leaves the bell without its badge.

test('signed in, the bell shows the inbox unread count, read once with the bearer', async ({
	context,
	page,
}, testInfo) => {
	const token = `${TOKEN.member}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, token, { 'GET /management/v1/me': ME_MEMBER });
	const reads: (string | undefined)[] = [];
	page.on('request', (req) => {
		if (new URL(req.url()).pathname === '/api/notifications/inbox') {
			reads.push(req.headers()['authorization']);
		}
	});

	await page.goto('/projects');

	await expect(
		page.getByRole('button', { name: `Notifications, ${MOCK_INBOX.unread} unread` }),
	).toBeVisible();
	// The badge shows on the first answer; a second read would follow it, so the count is taken once
	// the page has gone quiet.
	await page.waitForLoadState('networkidle');
	expect(reads).toEqual([`Bearer ${token}`]);
});
