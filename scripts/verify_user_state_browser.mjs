/**
 * Condition 5: a user's saved views follow the USER, not the browser.
 *
 * The bar is deliberately awkward to fake — write in one browser context, read it back in a genuinely
 * fresh one. A fresh `browser.newContext()` has its own cookie jar AND its own `localStorage`, so a view
 * that appears in the second context cannot have come from the first one's disk. That distinction is the
 * whole feature: these lived in `localStorage`, and the same signed-in person on another machine found an
 * empty list.
 *
 * Three checks:
 *   1. alice saves a distinctively-named view in context A.
 *   2. a FRESH context B, same person, sees it — proving the server holds it, not the browser.
 *   3. bob in context C does NOT see it — proving the store is keyed on the verified subject.
 *
 * Run:  node scripts/verify_user_state_browser.mjs
 */
import { chromium } from '@playwright/test';

const ORIGIN = 'http://localhost:8090';
const SHOT = '/home/blackwell/Desktop/lance-ns/docs/audits/shots';
// Distinctive, so a stale fixture or a coincidental match cannot pass this.
const VIEW = `cond5-${Date.now().toString(36)}`;

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
	if (page.url().includes('auth=error')) throw new Error(`login failed for ${user}: ${page.url()}`);
	return page;
}

/** Open the saved-views popover and return the names it lists. */
async function openSavedViews(page) {
	// By title, not by role-name: the trigger's content is "Views" (+ a count badge), and accessible-name
	// computation prefers content over `title`, so a /saved views/ name matcher never resolves.
	await page.getByTitle('Saved views').first().click();
	await page.waitForTimeout(1500); // the store's first read
	// The popover is PORTALLED to the body, so the page's own text already contains it. Reading the page
	// rather than a panel locator keeps this honest about what a user can actually see on screen.
	const text = await page.locator('body').innerText();
	return { panel: page, text };
}

let failures = 0;
const check = (label, ok, detail = '') => {
	console.log(`   ${ok ? '✓' : '✗'} ${label}${detail ? ` — ${detail}` : ''}`);
	if (!ok) failures += 1;
};

// ── 1. alice saves a view in context A ────────────────────────────────────────────────────────────
const a = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const alice1 = await login(a, 'alice@example.com', '/media/');
await alice1.waitForTimeout(2500);

await openSavedViews(alice1);
await alice1.getByPlaceholder(/save current view/i).fill(VIEW);
await alice1.getByTitle(/save view/i).first().click();
await alice1.waitForTimeout(1500);
const afterSave = await alice1.locator('body').innerText();
check('alice sees her new view in the context that saved it', afterSave.includes(VIEW), VIEW);
await alice1.screenshot({ path: `${SHOT}/cond5-alice-saved.png` });

// The proof that it left the browser: it is in the SERVER's copy for this subject.
const served = await alice1.evaluate(async () => {
	const r = await fetch('/media/capi/v1/user-state/saved-views');
	return { status: r.status, body: await r.text() };
});
check('the server holds it', served.status === 200 && served.body.includes(VIEW), `HTTP ${served.status}`);
await a.close();

// ── 2. a FRESH context — new cookie jar AND new localStorage ──────────────────────────────────────
const b = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const alice2 = await login(b, 'alice@example.com', '/media/');
await alice2.waitForTimeout(2500);
const mirrorEmpty = await alice2.evaluate(() => Object.keys(localStorage).length);
const { text: textB } = await openSavedViews(alice2);
check('a FRESH browser context shows the view', textB.includes(VIEW), `localStorage keys at load: ${mirrorEmpty}`);
await alice2.screenshot({ path: `${SHOT}/cond5-alice-fresh-context.png` });
await b.close();

// ── 3. bob must NOT see alice's work ──────────────────────────────────────────────────────────────
const c = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const bob = await login(c, 'bob@example.com', '/media/');
await bob.waitForTimeout(2500);
const { text: textC } = await openSavedViews(bob);
check("bob does NOT see alice's view", !textC.includes(VIEW));
await bob.screenshot({ path: `${SHOT}/cond5-bob-cannot-see.png` });
await c.close();

await browser.close();
console.log(failures === 0 ? '\n✓ condition 5 PROVEN' : `\n✗ ${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
