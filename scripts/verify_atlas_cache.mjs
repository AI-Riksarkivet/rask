/**
 * Condition 6: the atlas payload is filled ONCE and served to everyone allowed to see it — and to nobody
 * else, warm or cold.
 *
 * `/media/api/atlas/points` is 6,678,928 bytes. A tab-local memo fixed the space toggle; a refresh, a
 * second tab and a second user each still paid it in full. The server half caches the PAYLOAD, keyed on
 * the resource, and authorises every request before a byte is reachable.
 *
 * The three things that must hold together, because each one alone is satisfiable by a broken design:
 *   1. a second CALLER gets a hit — otherwise it is a per-user cache and buys nothing;
 *   2. an unauthenticated caller is refused even when the entry is WARM — a cache that serves what the
 *      authorisation would have refused is a disclosure, not an optimisation;
 *   3. a caller-supplied `v` cannot fork the key — measured before it was closed, six junk tokens evicted
 *      the product entry and twenty added twenty full upstream reads from one session.
 *
 * Run:  node scripts/verify_atlas_cache.mjs
 */
import { chromium } from '@playwright/test';

const ORIGIN = 'http://localhost:8090';
const POINTS = '/media/api/atlas/points?space=text&v=6';

const browser = await chromium.launch({
	args: ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'],
});

async function login(context, user) {
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent('/media/')}`, {
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

/** Fetch inside the page so the session cookie rides along; report status, cache marker and size. */
const probe = (page, path) =>
	page.evaluate(async (p) => {
		const r = await fetch(p);
		const body = await r.arrayBuffer();
		return { status: r.status, cache: r.headers.get('x-cache'), bytes: body.byteLength };
	}, path);

let failures = 0;
const check = (label, ok, detail = '') => {
	console.log(`   ${ok ? '✓' : '✗'} ${label}${detail ? ` — ${detail}` : ''}`);
	if (!ok) failures += 1;
};

// ── alice warms it ────────────────────────────────────────────────────────────────────────────────
const a = await browser.newContext();
const alice = await login(a, 'alice@example.com');
const first = await probe(alice, POINTS);
const second = await probe(alice, POINTS);
console.log(`   alice cold: ${first.status} ${first.cache} ${first.bytes}B`);
check('a repeat read is a hit', second.cache === 'hit', `${second.status} ${second.cache}`);

// ── bob, a DIFFERENT caller, must be served from the same entry ───────────────────────────────────
const b = await browser.newContext();
const bob = await login(b, 'bob@example.com');
const bobRead = await probe(bob, POINTS);
check(
	'a second CALLER is served warm — one fill for everyone allowed to see it',
	bobRead.status === 200 && bobRead.cache === 'hit',
	`${bobRead.status} ${bobRead.cache} ${bobRead.bytes}B`,
);

// ── the entry is warm; an unauthenticated caller must STILL be refused ────────────────────────────
const anon = await browser.newContext();
const anonPage = await anon.newPage();
await anonPage.goto(`${ORIGIN}/media/`, { waitUntil: 'domcontentloaded' }).catch(() => {});
const anonRead = await probe(anonPage, POINTS);
check(
	'an unauthenticated caller is refused WHILE the entry is warm',
	anonRead.status === 401 && anonRead.bytes < 1000,
	`${anonRead.status}, ${anonRead.bytes}B`,
);

// the same resource one URL away, in the other zone that mounts the viewer catch-all
const anonAnnotator = await probe(anonPage, '/annotator/api/atlas/points?space=text&v=6');
check(
	'and refused on the annotator zone too — gating a URL is not gating a resource',
	anonAnnotator.status === 401 && anonAnnotator.bytes < 1000,
	`${anonAnnotator.status}, ${anonAnnotator.bytes}B`,
);

// ── a caller-supplied `v` must not be able to fork the key ────────────────────────────────────────
const forks = [];
for (const junk of ['1', '99', 'x', 'zzz', String(Date.now())]) {
	forks.push(await probe(alice, `/media/api/atlas/points?space=text&v=${junk}`));
}
check(
	'junk `v` tokens cannot fork the cache',
	forks.every((f) => f.cache === 'hit'),
	forks.map((f) => f.cache).join(','),
);
const afterJunk = await probe(alice, POINTS);
check('and the product entry survives them', afterJunk.cache === 'hit', String(afterJunk.cache));

await browser.close();
console.log(failures === 0 ? '\n✓ condition 6 PROVEN' : `\n✗ ${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
