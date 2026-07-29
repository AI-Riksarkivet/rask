// Live proof for the edge's stream timeout: a SvelteKit `query.live` subscription, opened through the
// ingress as a real signed-in user, must survive longer than nginx's 60s default `proxy_read_timeout`.
//
// Why this needs a clock and not a manifest read. ingress-nginx measures that timeout as IDLE time on the
// upstream connection, and the lakehouse zone's `controlEvents` generator yields ONLY when the control-plane
// feed changes — the correct discipline, and exactly the shape the default kills. SvelteKit's SSE transport
// sends no keepalive (kit 2.70.1 runtime/server/remote.js has one enqueue, the payload), so nothing else
// refreshes the clock. Without the Ingress annotation the browser is cut off at ~60s and kit's client
// silently reconnects: the subscription LOOKS live while costing a full re-subscribe every minute.
//
// So the observation is: hold one page open past 60s of quiet and count the requests kit makes to
// /_app/remote/. One request still in flight at the end = the stream survived. A second request = the edge
// severed the first and the client reconnected — the failure this exists to catch.
//
// Prereqs (same as verify_cross_zone_oidc.mjs): zones deployed OIDC-on behind ingress-nginx, alice granted
// admin on project:acme, ingress + Dex port-forwarded. Env: ORIGIN, HOLD_S, SHOT_DIR.

import { mkdirSync } from 'node:fs';
import { chromium } from '@playwright/test';

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8090';
const HOLD_S = Number(process.env.HOLD_S ?? 100);
const SHOT_DIR = process.env.SHOT_DIR ?? '/tmp/lance-verify-shots';
// The bar this run actually clears. nginx severs an idle stream at its 60s default; Bun's own
// IDLE_TIMEOUT is the harder one at 255s, and only a hold longer than that proves the keepalive.
const BAR_S = HOLD_S > 255 ? 255 : 60;
const ARGS = ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'];
const PAGE = '/lakehouse/admin/events'; // the one page driven by a query.live subscription

const t0 = Date.now();
const at = () => `t+${((Date.now() - t0) / 1000).toFixed(1)}s`;
const log = (msg) => console.log(`   [${at()}] ${msg}`);

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

const browser = await chromium.launch({ args: ARGS });
const context = await browser.newContext();
console.log(`→ alice signs in and opens ${PAGE} (holding ${HOLD_S}s, clearing the ${BAR_S}s bar)`);
const page = await login(context, 'alice@example.com', PAGE);

// Every request kit makes for the live subscription, with the second it started and how it ended.
const streams = [];
page.on('request', (req) => {
	if (!req.url().includes('/_app/remote/')) return;
	const rec = { url: req.url(), startedAt: Date.now(), endedAt: null, how: null };
	streams.push(rec);
	log(`stream #${streams.length} opened → ${new URL(req.url()).pathname}`);
});
const end = (how) => (req) => {
	if (!req.url().includes('/_app/remote/')) return;
	const rec = streams.find((s) => s.url === req.url() && s.endedAt === null);
	if (!rec) return;
	rec.endedAt = Date.now();
	rec.how = how;
	log(`stream closed after ${((rec.endedAt - rec.startedAt) / 1000).toFixed(1)}s (${how})`);
};
page.on('requestfinished', end('finished'));
page.on('requestfailed', end('failed'));

await page.goto(`${ORIGIN}${PAGE}`, { waitUntil: 'domcontentloaded' });
log(`on ${page.url()}`);

for (let s = 10; s <= HOLD_S; s += 10) {
	await page.waitForTimeout(10_000);
	const open = streams.filter((r) => r.endedAt === null).length;
	log(`holding — ${streams.length} stream request(s) so far, ${open} still open`);
}

mkdirSync(SHOT_DIR, { recursive: true });
const shotFile = `${SHOT_DIR}/live-stream-past-${BAR_S}s.png`;
await page.screenshot({ path: shotFile, fullPage: true });
console.log(`     shot: ${shotFile}`);

const first = streams[0];
console.log(`\n   streams observed over ${HOLD_S}s:`);
for (const [i, s] of streams.entries()) {
	const age = ((s.endedAt ?? Date.now()) - s.startedAt) / 1000;
	console.log(
		`     #${i + 1} opened at t+${((s.startedAt - t0) / 1000).toFixed(1)}s, ` +
			`${s.endedAt ? `closed after ${age.toFixed(1)}s (${s.how})` : `STILL OPEN after ${age.toFixed(1)}s`}`,
	);
}

ok(first !== undefined, 'the page opened a live-query stream at all');
if (first) {
	const age = ((first.endedAt ?? Date.now()) - first.startedAt) / 1000;
	// A MARGIN, not `> 60`. Measured with only the edge reverted, a 60s `proxy_read_timeout` severs the
	// stream at 61.0s — which sails through a bare "past 60s" check while the estate is broken. The
	// assertion has to sit far enough past the timeout that the timeout cannot satisfy it.
	ok(age > 75, `the first stream lived ${age.toFixed(1)}s — clear of nginx's 60s default`);
	ok(first.endedAt === null, 'the first stream is still open at the end of the hold');
}
// Churn is a stream that was SEVERED and replaced, not the number of subscriptions a page opens. This
// asserted `length === 1` and went red once the notification bell was mounted in the root layout: the
// admin page legitimately holds two concurrent streams (its own controlEvents, plus the bell's lineage
// feed), both healthy. Counting closures measures the thing the check is actually named for — and it
// still catches the original failure, where the first stream ended and a second appeared to replace it.
const severed = streams.filter((s) => s.endedAt !== null);
ok(
	severed.length === 0,
	`no stream was severed during ${HOLD_S}s — ${streams.length} opened, ${severed.length} closed`,
);

await browser.close();
console.log(failures ? `\n✗ ${failures} check(s) failed` : `\n✓ the live stream survived past ${BAR_S}s`);
process.exit(failures ? 1 : 0);
