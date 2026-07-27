// Live proof that per-subject user state is reachable from a REAL browser session through the REAL edge,
// and that two signed-in people get two different answers from one URL.
//
// The pytest suite beside this drives the catalog directly with a bearer. That is the complete backend
// proof, and it is not the same claim: it does not exercise the ingress, the zone's `/capi` BFF, or the
// sealed session cookie the browser actually carries. This does — alice and bob each sign in at Dex
// through :8090 and read `/lakehouse/capi/v1/user-state/workflow-graph` with nothing but their session.
//
// Read half only, and deliberately: the lakehouse zone's catch-all `capi/[...path]/+server.ts` exports
// GET and nothing else, so the browser cannot PUT yet. Writing from a zone is the frontend half of this
// condition and is not in this change's scope — the drive says what exists rather than implying more.
//
// Env: ORIGIN (default http://localhost:8090), SHOT_DIR (default docs/audits/shots).

import { mkdirSync } from 'node:fs';
import { chromium } from '@playwright/test';

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8090';
const SHOT_DIR = process.env.SHOT_DIR ?? 'docs/audits/shots';
const API = '/lakehouse/capi/v1/user-state/workflow-graph';
const ARGS = ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'];

let failures = 0;
const ok = (cond, msg) => {
	console.log(`   ${cond ? '✓' : '✗ FAIL:'} ${msg}`);
	if (!cond) failures++;
};

async function login(context, user) {
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent('/lakehouse')}`, {
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

// Signed OUT: the BFF must answer 401, not redirect an API path to the login page.
console.log('→ signed out');
const anonCtx = await browser.newContext();
const anon = await anonCtx.request.get(`${ORIGIN}${API}`);
console.log(`   GET ${API} → ${anon.status()}`);
ok(anon.status() === 401, 'an unauthenticated read is 401 through the edge');

const seen = {};
for (const user of ['alice@example.com', 'bob@example.com']) {
	const who = user.split('@')[0];
	console.log(`→ ${who} signs in at Dex and reads her/his own document`);
	const ctx = await browser.newContext();
	const page = await login(ctx, user);

	const res = await ctx.request.get(`${ORIGIN}${API}`);
	const body = await res.json();
	console.log(`   GET ${API} → ${res.status()}`);
	console.log(
		`   subject=${body.subject}  exists=${body.exists}  canvas=${
			body.value?.config?.q1?.q ?? '(none)'
		}`,
	);
	ok(res.status() === 200, `${who} gets 200 through the zone BFF with only a session cookie`);
	ok(typeof body.subject === 'string' && body.subject.length > 0, `${who}'s subject is echoed`);
	seen[who] = body;

	// Screenshot the response as the browser renders it — this is the surface that exists today.
	await page.goto(`${ORIGIN}${API}`, { waitUntil: 'domcontentloaded' });
	await page.screenshot({ path: `${SHOT_DIR}/user-state-${who}.png`, fullPage: true });
	await ctx.close();
}

ok(
	seen.alice.subject !== seen.bob.subject,
	`two signed-in people are two subjects (${seen.alice.subject?.slice(0, 10)}… vs ${seen.bob.subject?.slice(0, 10)}…)`,
);
const aliceCanvas = seen.alice.value?.config?.q1?.q;
const bobCanvas = seen.bob.value?.config?.q1?.q;
ok(
	!(seen.alice.exists && seen.bob.exists) || aliceCanvas !== bobCanvas,
	`neither is served the other's canvas (alice="${aliceCanvas}" bob="${bobCanvas}")`,
);

await browser.close();
console.log(failures === 0 ? '\n✓ user state is per-subject through the real edge' : `\n${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
