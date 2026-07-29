// Live proof that `GET /v1/table/{id}/history` is DEPLOYED, not merely committed: the endpoint answered
// through the public origin for a signed-in user, and the History tab rendered those versions in a browser.
//
// The failure this exists to catch is the one that happened: the endpoint was committed at 14:48 and the
// catalog pod had been running since 13:27, so every unit test passed while the live estate returned 404.
// A view built against a contract nobody served renders an empty state and looks like "no versions yet".
//
// Driven through :8090 (ingress → the lakehouse zone's `/capi` BFF → catalog), never against the pod's own
// port, because the BFF's bearer-forwarding is part of what has to work. Both users are driven: alice reads
// her table, bob is refused on the same URL, so the output distinguishes "deployed" from "deployed and
// ungoverned".
//
// Env: ORIGIN, TABLE (a table id alice can read), SHOT_DIR, COOKIE_OUT (where to drop alice's session
// cookie so a literal curl can repeat the request outside the browser).

import { mkdirSync, writeFileSync } from 'node:fs';
import { chromium } from '@playwright/test';

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8090';
const TABLE = process.env.TABLE ?? 'mbns$histprobe';
const SHOT_DIR = process.env.SHOT_DIR ?? '/tmp/lance-verify-shots';
const COOKIE_OUT = process.env.COOKIE_OUT ?? '';
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

const enc = encodeURIComponent(TABLE);
const detailPath = `/lakehouse/data/tables/${enc}?tab=history`;
const apiPath = `/lakehouse/capi/v1/table/${enc}/history?limit=20`;
const browser = await chromium.launch({ args: ARGS });

// 1. alice — the endpoint answers through the edge, and the tab renders what it answered.
console.log(`→ alice reads the commit log of ${TABLE}`);
const aliceCtx = await browser.newContext();
const alice = await login(aliceCtx, 'alice@example.com', detailPath);

const res = await aliceCtx.request.get(`${ORIGIN}${apiPath}`);
const body = await res.text();
console.log(`   GET ${apiPath} → ${res.status()}`);
ok(res.status() === 200, `alice gets 200 from the history endpoint through :8090`);
let rows = [];
try {
	rows = JSON.parse(body).versions ?? [];
} catch {
	/* leave rows empty — the assertion below reports it */
}
console.log(
	`   versions: ${rows.map((r) => `v${r.version} ${r.operation}`).join(', ') || '(none)'}`,
);
const del = rows.find((r) => r.predicate);
if (del) console.log(`   the delete predicate as written: ${del.operation} "${del.predicate}"`);
ok(rows.length > 0, `the response carries real versions (${rows.length})`);

await alice.goto(`${ORIGIN}${detailPath}`, { waitUntil: 'domcontentloaded' });

// Read the TAB PANEL, never the whole page. The first version of this check asserted the newest version
// number appeared anywhere in <main>, and it passed against a zone image that had no History tab at all —
// it was matching the "v10" badge beside the table's title while the page sat on Overview. A screenshot
// caught it. So: the tab must exist, and the assertions must read the panel the tab reveals.
const historyTab = alice.getByRole('tab', { name: /^history$/i });
// `count()` does not auto-wait: asking before hydration reports zero tabs on a page that has four.
await alice.getByRole('tab').first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
const tabNames = await alice.getByRole('tab').allTextContents();
console.log(`   tab bar: ${tabNames.map((t) => t.trim()).join(' | ')}`);
ok((await historyTab.count()) === 1, 'the detail page has a History tab');
if ((await historyTab.count()) === 1) await historyTab.click();

// Wait for the LOG, not for its container. The rows appear immediately from the detail aggregate's
// manifest list with an empty operation column, and the transaction detail arrives one fetch later — so a
// screenshot taken when the table becomes visible catches a page of em dashes and an assertion on it fails
// for a reason that has nothing to do with the endpoint. Wait until the newest version's operation, the
// thing only the history endpoint can supply, is on screen.
const scope = alice.locator('main').first();
await scope
	.getByText(rows[0]?.operation ?? 'Overwrite', { exact: false })
	.first()
	.waitFor({ state: 'visible', timeout: 20000 })
	.catch(() => {});
const text = ((await scope.textContent()) ?? '').replace(/\s+/g, ' ');
mkdirSync(SHOT_DIR, { recursive: true });
await alice.screenshot({ path: `${SHOT_DIR}/history-alice.png`, fullPage: true });
console.log(`     shot: ${SHOT_DIR}/history-alice.png`);

// One row per version: the rendered log must name every version the API returned.
const missing = rows.map((r) => r.version).filter((v) => !new RegExp(`\\bv?${v}\\b`).test(text));
ok(missing.length === 0, `every version the API returned is rendered (missing: ${missing.join(', ')})`);
for (const op of [...new Set(rows.map((r) => r.operation))]) {
	ok(text.toLowerCase().includes(op.toLowerCase()), `the rendered log names the ${op} operation`);
}
// The delete predicate is the point of the feature: "rows matching `id = 2` went away" is an answer.
// It sits at the bottom of the log, and the shell scrolls an inner container so `fullPage` does not reach
// it — scroll the row into view and take a second shot, or the evidence stops at the fold.
if (del) {
	ok(text.includes(del.predicate), `the delete predicate "${del.predicate}" is rendered verbatim`);
	const row = scope.locator('tr', { hasText: del.predicate }).first();
	await row.scrollIntoViewIfNeeded({ timeout: 5000 }).catch(() => {});
	await alice.screenshot({ path: `${SHOT_DIR}/history-alice-predicate.png` });
	console.log(`     shot: ${SHOT_DIR}/history-alice-predicate.png`);
}

if (COOKIE_OUT) {
	const cookies = await aliceCtx.cookies(ORIGIN);
	writeFileSync(COOKIE_OUT, cookies.map((c) => `${c.name}=${c.value}`).join('; '));
	console.log(`     alice's cookie written to ${COOKIE_OUT} (for a literal curl of the same URL)`);
}

// 2. bob — same URL, no access. A deployed endpoint that answers everyone is a worse bug than a 404.
console.log(`\n→ bob asks for the same commit log`);
const bobCtx = await browser.newContext();
await login(bobCtx, 'bob@example.com', '/lakehouse/data');
const bobRes = await bobCtx.request.get(`${ORIGIN}${apiPath}`);
console.log(`   GET ${apiPath} → ${bobRes.status()}`);
ok(bobRes.status() === 403, `bob is refused (${bobRes.status()})`);

await browser.close();
console.log(failures ? `\n✗ ${failures} check(s) failed` : `\n✓ the history endpoint is live`);
process.exit(failures ? 1 : 0);
