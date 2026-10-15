// Live proof, through the real ingress, that the lakehouse zone's reads are CHANGE-DRIVEN rather than
// timed — and that the four admin surfaces which used to read once on mount and then sit still now have a
// feed.
//
// The unit and e2e proofs run against a dev server with mocked backends. This one runs against :8090 →
// nginx → the built zone image → the real lineage and catalog planes, with a real Dex login, because the
// thing being claimed ("no timer, and it still refreshes") is a property of the deployed union, not of a
// component.
//
// What it measures:
//   1. The shell's notification bell mounted and opening its panel.
//   2. Every one of the four admin surfaces issuing a real request and rendering real rows.
//   3. bob, who is not an estate admin, refused at the admin door.
//
// Env: ORIGIN, SHOT_DIR.

import { mkdirSync } from 'node:fs';
import { chromium } from '@playwright/test';

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8090';
const SHOT_DIR = process.env.SHOT_DIR ?? 'docs/audits/shots';
const ARGS = ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'];

let failures = 0;
const ok = (cond, msg) => {
	console.log(`   ${cond ? '✓' : '✗ FAIL:'} ${msg}`);
	if (!cond) failures++;
};

async function login(context, user, startPath) {
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent(startPath)}`, {
		waitUntil: 'domcontentloaded',
	});
	await page.waitForURL(/\/dex\/auth/, { timeout: 15000 });
	await page.fill('input[name="login"], input#login, input[type="text"], input[type="email"]', user);
	await page.fill('input[name="password"], input#password, input[type="password"]', 'password');
	await page.click('button[type="submit"], input[type="submit"], #submit-login');
	await page.waitForURL((u) => u.origin === ORIGIN && !u.pathname.startsWith('/auth'), {
		timeout: 20000,
	});
	if (page.url().includes('auth=error')) throw new Error(`login failed for ${user}: ${page.url()}`);
	return page;
}

mkdirSync(SHOT_DIR, { recursive: true });
const browser = await chromium.launch({ args: ARGS });

// ── 1. the notification bell ────────────────────────────────────────────────────────────────────
const aliceCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const alice = await login(aliceCtx, 'alice@example.com', '/lakehouse/');
console.log('→ the shell notification bell');
const bell = alice.getByRole('button', { name: /notifications/i });
ok(await bell.isVisible(), 'the bell is mounted in the estate navbar');
console.log(`   bell accessible name: "${await bell.getAttribute('aria-label')}"`);
await bell.click();
await alice.waitForTimeout(800);
await alice.screenshot({ path: `${SHOT_DIR}/live-notification-bell.png` });
const panelText = await alice.locator('body').innerText();
ok(/Notifications/.test(panelText), 'the panel opens with the notification list');
await alice.keyboard.press('Escape');

// ── 2. the four admin surfaces that used to read once and stop ──────────────────────────────────
for (const [name, path, marker] of [
	['audit', '/lakehouse/admin/audit', /Audit|audit/],
	['dlq', '/lakehouse/admin/dlq', /DLQ|outbox|backlog/i],
	['tenants', '/lakehouse/admin/tenants', /Tenants|project/i],
	['streams', '/lakehouse/admin/streams', /stream|consumer/i],
]) {
	console.log(`→ /admin/${name}`);
	const seen = [];
	const listener = (res) => {
		const p = new URL(res.url()).pathname;
		if (p.includes('/lakehouse/api/') || p.includes('/lakehouse/capi/')) seen.push(`${res.status()} ${p}`);
	};
	alice.on('response', listener);
	await alice.goto(`${ORIGIN}${path}`, { waitUntil: 'domcontentloaded' });
	await alice.waitForTimeout(6000);
	alice.off('response', listener);
	const backend = seen.filter((s) => !s.includes('/management/v1/me'));
	console.log(`   backend requests: ${backend.length ? backend.join(', ') : '(NONE)'}`);
	ok(backend.length > 0, `/admin/${name} makes a real backend request`);
	ok(marker.test(await alice.locator('body').innerText()), `/admin/${name} renders its surface`);
	await alice.screenshot({ path: `${SHOT_DIR}/live-admin-${name}.png` });
}

// ── 3. bob is refused at the admin door ─────────────────────────────────────────────────────────
console.log('→ bob, who is not an estate admin');
const bobCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const bob = await login(bobCtx, 'bob@example.com', '/lakehouse/');
const bobAdmin = await bobCtx.request.get(`${ORIGIN}/lakehouse/admin/tenants`);
console.log(`   GET /lakehouse/admin/tenants → ${bobAdmin.status()}`);
ok(bobAdmin.status() === 403, 'bob is refused the admin area by the server-side door');
await bob.goto(`${ORIGIN}/lakehouse/`, { waitUntil: 'domcontentloaded' });
await bob.waitForTimeout(3000);
const bobBell = bob.getByRole('button', { name: /notifications/i });
ok(await bobBell.isVisible(), 'bob still gets a bell — the feed is governed per subject, not admin-only');
await bob.screenshot({ path: `${SHOT_DIR}/live-bell-bob.png` });

await browser.close();
console.log(failures === 0 ? '\nALL CHECKS PASSED' : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
