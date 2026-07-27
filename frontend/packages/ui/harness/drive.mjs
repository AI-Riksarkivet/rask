// Drives the notification centre in a real browser: opens the portalled panel, reads what a user
// would read, dismisses a row, closes to clear the unread count, then re-opens in dark mode and at
// phone width. Writes the screenshots this work is evidenced by. See README.md for how to serve it.
//
import { chromium } from '@playwright/test';

const SHOTS = '/home/blackwell/Desktop/lance-ns/docs/audits/shots';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 620 } });
const fails = [];
const ok = (cond, msg) => {
	console.log(`   ${cond ? '✓' : '✗ FAIL:'} ${msg}`);
	if (!cond) fails.push(msg);
};

await page.goto('http://localhost:5411/', { waitUntil: 'networkidle' });

// 1. Closed bell — the count must be legible without opening anything.
const bell = page.getByRole('button', { name: /Notifications/ });
await bell.waitFor();
ok(
	(await bell.getAttribute('aria-label')) === 'Notifications, 5 unread',
	`bell label: ${await bell.getAttribute('aria-label')}`,
);
await page.screenshot({ path: `${SHOTS}/notifications-bell-closed.png` });

// 2. Open the panel — this is the path SSR cannot render (portalled content).
await bell.click();
const panel = page.getByRole('dialog');
await panel.waitFor();
const rows = panel.locator('[data-slot="notification"]');
ok((await rows.count()) === 5, `rows rendered: ${await rows.count()}`);
const panelText = (await panel.innerText()).replace(/\n+/g, ' | ');
console.log('   panel text:', panelText);
ok(
	panelText.includes('ValueError: embedding dim 384 != table dim 768'),
	'the failed run shows its error text',
);
ok(panelText.includes('2 of 5'), 'the running run shows its progress');
ok(
	panelText.includes('Started') && panelText.includes('Completed') && panelText.includes('Failed'),
	'START / COMPLETE / FAIL all rendered',
);
await page.screenshot({ path: `${SHOTS}/notifications-panel-open.png` });

// 3. Dismiss one row — the count and the list must both drop.
await panel.getByRole('button', { name: 'Dismiss embed_features' }).first().click();
await page.waitForTimeout(150);
ok((await rows.count()) === 4, `rows after one dismiss: ${await rows.count()}`);
await page.screenshot({ path: `${SHOTS}/notifications-after-dismiss.png` });

// 4. Close the panel — read-on-close clears the badge.
await page.keyboard.press('Escape');
await page.waitForTimeout(250);
ok(
	(await bell.getAttribute('aria-label')) === 'Notifications',
	`bell after close: ${await bell.getAttribute('aria-label')}`,
);
await page.screenshot({ path: `${SHOTS}/notifications-read.png` });

// 5. Dark mode, panel open.
await page.evaluate(() => document.documentElement.classList.add('dark'));
await bell.click();
await panel.waitFor();
await page.screenshot({ path: `${SHOTS}/notifications-panel-dark.png` });

// 6. Narrow viewport — the panel must not spill off a phone-width screen.
await page.keyboard.press('Escape');
await page.evaluate(() => document.documentElement.classList.remove('dark'));
await page.setViewportSize({ width: 380, height: 700 });
await bell.click();
await panel.waitFor();
const box = await panel.boundingBox();
ok(
	box.x >= 0 && box.x + box.width <= 380,
	`panel within 380px viewport: x=${box.x.toFixed(1)} w=${box.width.toFixed(1)}`,
);
await page.screenshot({ path: `${SHOTS}/notifications-panel-narrow.png` });

await browser.close();
console.log(fails.length ? `\n${fails.length} FAILED` : '\nall checks passed');
process.exit(fails.length ? 1 : 0);
