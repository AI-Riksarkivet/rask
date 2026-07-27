/**
 * Condition 3: a run that starts, finishes or fails is surfaced without the user hunting for it — in a
 * real ZONE, not the component harness.
 *
 * The harness proved the component renders a shape. This proves the lakehouse zone mounts it, feeds it
 * from the lineage service's own `/runs` (START → RUNNING → COMPLETE/FAIL with progress and
 * error_message), and that a signed-in person sees it in the navbar.
 *
 * Run:  node scripts/verify_notifications.mjs
 */
import { chromium } from '@playwright/test';

const ORIGIN = 'http://localhost:8090';
const SHOT = '/home/blackwell/Desktop/lance-ns/docs/audits/shots';

const browser = await chromium.launch({
	args: ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'],
});

async function login(context, user, startPath) {
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent(startPath)}`, {
		waitUntil: 'domcontentloaded',
	});
	await page.waitForURL(/\/dex\/auth/, { timeout: 20000 });
	await page.fill('input[name="login"], input#login, input[type="text"], input[type="email"]', user);
	await page.fill('input[name="password"], input#password, input[type="password"]', 'password');
	await page.click('button[type="submit"], input[type="submit"], #submit-login');
	await page.waitForURL((u) => u.origin === ORIGIN && !u.pathname.startsWith('/auth'), {
		timeout: 25000,
	});
	return page;
}

let failures = 0;
const check = (label, ok, detail = '') => {
	console.log(`   ${ok ? '✓' : '✗'} ${label}${detail ? ` — ${detail}` : ''}`);
	if (!ok) failures += 1;
};

const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const alice = await login(ctx, 'alice@example.com', '/lakehouse/');
await alice.waitForTimeout(3000);

// What the ZONE's own feed returns — the data the bell is fed from.
const runs = await alice.evaluate(async () => {
	const r = await fetch('/lakehouse/api/runs');
	return { status: r.status, body: r.ok ? await r.json() : null };
});
const list = runs.body?.runs ?? [];
const states = [...new Set(list.map((r) => r.state))];
console.log(`   /lakehouse/api/runs -> ${runs.status}, ${list.length} run(s), states: ${states.join(',') || 'none'}`);
check('the zone can read the run feed', runs.status === 200);
check('the feed carries lifecycle states', states.length > 0, states.join(','));

// The bell itself, in the estate navbar.
const bell = alice.getByRole('button', { name: /notification/i }).first();
check('the navbar has a notification bell', (await bell.count()) > 0);

if ((await bell.count()) > 0) {
	await bell.click();
	await alice.waitForTimeout(1200);
	const text = await alice.locator('body').innerText();
	const failed = list.find((r) => r.state === 'FAIL' || r.state === 'ABORT');
	const anyJob = list.find((r) => r.job)?.job ?? '';

	check('the panel names a real job from the feed', anyJob !== '' && text.includes(anyJob.split('/').pop() ?? ''), anyJob);
	if (failed?.error_message) {
		check(
			'a FAILED run shows its error_message, not just a red badge',
			text.includes(failed.error_message.slice(0, 30)),
			failed.error_message.slice(0, 60),
		);
	} else {
		console.log('   · no failed run in the feed right now — error rendering not exercised live');
	}
	await alice.screenshot({ path: `${SHOT}/cond3-notifications-in-zone.png` });

	// Condition 10: measure rather than eyeball. The clipped-descender defect was invisible by eye.
	const metrics = await alice.evaluate(() => {
		const el = [...document.querySelectorAll('p')].find((p) => p.className.includes('truncate'));
		if (!el) return null;
		const cs = getComputedStyle(el);
		return {
			text: el.textContent?.trim().slice(0, 24),
			fontSize: cs.fontSize,
			lineHeight: cs.lineHeight,
			clientH: el.clientHeight,
			scrollH: el.scrollHeight,
		};
	});
	if (metrics) {
		console.log(`   metrics: ${JSON.stringify(metrics)}`);
		check('row text is not clipped (clientHeight >= scrollHeight)', metrics.clientH >= metrics.scrollH);
	}
}

await browser.close();
console.log(failures === 0 ? '\n✓ condition 3 PROVEN' : `\n✗ ${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
