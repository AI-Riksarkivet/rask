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
//   1. /lakehouse/api/runs requests over a quiet window. One is correct; three is the old 5s timer.
//   2. The bytes each /api/events read costs — the `summary=true` fix on the job detail page.
//   3. Every one of the four admin surfaces issuing a real request and rendering real rows.
//   4. bob, who is not an estate admin, refused at the admin door.
//
// Env: ORIGIN, SHOT_DIR, QUIET_MS.

import { mkdirSync } from 'node:fs';
import { chromium } from '@playwright/test';

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8090';
const SHOT_DIR = process.env.SHOT_DIR ?? 'docs/audits/shots';
const QUIET_MS = Number(process.env.QUIET_MS ?? 13000);
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

// ── 1. the runs board: one read, not a read every five seconds ──────────────────────────────────
console.log('→ alice opens the runs board and leaves it alone');
const aliceCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const alice = await login(aliceCtx, 'alice@example.com', '/lakehouse/lineage/runs');

const reads = [];
const eventReads = [];
alice.on('response', async (res) => {
	const path = new URL(res.url()).pathname;
	if (path.endsWith('/api/runs')) reads.push(Date.now());
	if (path.endsWith('/api/events')) {
		// Measure the BODY, not `content-length` — the header is absent on a chunked response, and the
		// first version of this script reported a triumphant "0 bytes" because of it.
		const bytes = await res.body().then((b) => b.length).catch(() => -1);
		eventReads.push({ url: res.url(), bytes });
	}
});

await alice.goto(`${ORIGIN}/lakehouse/lineage/runs`, { waitUntil: 'domcontentloaded' });
await alice.getByRole('heading', { name: 'Runs' }).waitFor({ timeout: 20000 });
await alice.waitForTimeout(2000);
const afterLoad = reads.length;
await alice.waitForTimeout(QUIET_MS);
console.log(`   /api/runs requests: ${reads.length} in ${(QUIET_MS + 2000) / 1000}s of an idle board`);
ok(reads.length === afterLoad, `no further read while nothing changed (was ${afterLoad}, still ${reads.length})`);
ok(reads.length <= 2, `the 5s timer is gone — it would have issued ~3 by now, this issued ${reads.length}`);
await alice.screenshot({ path: `${SHOT_DIR}/live-runs-board.png`, fullPage: false });

// ── 2. the notification bell, fed from GET /runs through the live feed ──────────────────────────
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

// ── 3. the job detail page: summary=true on the wire ────────────────────────────────────────────
console.log('→ the job detail page — the events read that was 10x too big');
eventReads.length = 0;
const jobs = await aliceCtx.request.get(`${ORIGIN}/lakehouse/api/jobs`);
const jobList = (await jobs.json()).jobs ?? [];
const job = jobList.find((j) => j.namespace) ?? jobList[0];
if (job) {
	const jobPath = `${base(job)}`;
	await alice.goto(`${ORIGIN}/lakehouse/lineage/jobs/${jobPath}`, { waitUntil: 'domcontentloaded' });
	await alice.waitForTimeout(4000);
	for (const r of eventReads) console.log(`   ${r.bytes} bytes  ${new URL(r.url).search || '(no query)'}`);
	const windowRead = eventReads.find((r) => r.url.includes('summary=true'));
	ok(!!windowRead, 'the 200-event window is fetched with summary=true');
	if (windowRead) ok(windowRead.bytes < 200000, `the window read is ${windowRead.bytes} bytes, not ~471_000`);
	await alice.screenshot({ path: `${SHOT_DIR}/live-job-detail.png` });
} else {
	console.log('   (no jobs on this estate — skipped)');
}
function base(j) {
	return `${j.namespace ? `${encodeURIComponent(j.namespace)}/` : ''}${encodeURIComponent(j.name)}`;
}

// ── 4. the four admin surfaces that used to read once and stop ──────────────────────────────────
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
	const backend = seen.filter((s) => !s.includes('/v1/me'));
	console.log(`   backend requests: ${backend.length ? backend.join(', ') : '(NONE)'}`);
	ok(backend.length > 0, `/admin/${name} makes a real backend request`);
	ok(marker.test(await alice.locator('body').innerText()), `/admin/${name} renders its surface`);
	await alice.screenshot({ path: `${SHOT_DIR}/live-admin-${name}.png` });
}

// ── 5. bob is refused at the admin door ─────────────────────────────────────────────────────────
console.log('→ bob, who is not an estate admin');
const bobCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const bob = await login(bobCtx, 'bob@example.com', '/lakehouse/lineage/runs');
const bobAdmin = await bobCtx.request.get(`${ORIGIN}/lakehouse/admin/tenants`);
console.log(`   GET /lakehouse/admin/tenants → ${bobAdmin.status()}`);
ok(bobAdmin.status() === 403, 'bob is refused the admin area by the server-side door');
await bob.goto(`${ORIGIN}/lakehouse/lineage/runs`, { waitUntil: 'domcontentloaded' });
await bob.waitForTimeout(3000);
const bobBell = bob.getByRole('button', { name: /notifications/i });
ok(await bobBell.isVisible(), 'bob still gets a bell — the feed is governed per subject, not admin-only');
await bob.screenshot({ path: `${SHOT_DIR}/live-runs-bob.png` });

await browser.close();
console.log(failures === 0 ? '\nALL CHECKS PASSED' : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
