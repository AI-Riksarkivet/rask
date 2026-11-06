/**
 * What the console's live verify scripts share: a person signed in to the console through Dex, as the console signs
 * in (authorization code with PKCE on its public client), and a guard that keeps every write from leaving unless the
 * run was started with `--write`.
 *
 * THE BEARER IS THE CONSOLE'S OWN. The console keeps its tokens in memory, so the drive reads the one it attaches to
 * its own `/api/*` calls off the request, and makes its direct API calls with that: no password grant, no client
 * secret, and a token whose scopes are exactly those `/config.json` asks for. A token is never printed.
 *
 * A RELOAD SIGNS IN AGAIN. A full navigation drops the in-memory tokens by design, so `reload` goes back through Dex
 * and fills the form again when Dex asks for it, and in-app moves use `visit`, a soft navigation.
 *
 * THE WRITE GUARD IS METHOD-BASED. Without `--write`, every request to `/api/*` whose method is not GET, HEAD or
 * OPTIONS is aborted in the browser and refused in `api()`, and each attempt is recorded so the run reports it: the
 * console's own read-shaped POSTs (a Lance Namespace describe, a decision-door check) are refused too, which is the
 * price of a guard that cannot be argued with.
 */
import { chromium } from '@playwright/test';

export const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8080';
export const PASSWORD = process.env.DEX_PASSWORD ?? 'password';
/** Wall-clock bound on one sign-in or one wait. */
export const STEP_MS = Number(process.env.STEP_MS ?? 30_000);

const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/** The flags a script accepts. `--write` alone unlocks the writing steps. */
export function parseArgs(argv, usage) {
	const args = { write: false, values: {} };
	for (let i = 0; i < argv.length; i += 1) {
		const arg = argv[i];
		if (arg === '--help' || arg === '-h') {
			console.log(usage);
			process.exit(0);
		} else if (arg === '--write') args.write = true;
		else if (arg.startsWith('--')) {
			const [key, inline] = arg.slice(2).split('=', 2);
			const value = inline ?? argv[++i];
			if (value === undefined) throw new Error(`--${key} needs a value`);
			args.values[key] = value;
		} else throw new Error(`unknown argument ${arg}\n\n${usage}`);
	}
	return args;
}

/** A check that reports and counts, never throws: "it did not move" is itself the finding. */
export function checker() {
	const state = { failures: 0 };
	state.check = (label, ok, detail = '') => {
		console.log(`   ${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
		if (!ok) state.failures += 1;
	};
	return state;
}

export async function launch() {
	return chromium.launch();
}

/** The principal a Dex token proves: the chart's idp id for the development Dex (`dex`) and the user id Dex reports
 *  in `federated_claims`, which it does only for a client that asks for `federated:id`. It is the FGA principal, the
 *  inbox actor's id and the author `enforce_author` stamps. */
export function subjectOf(token) {
	const claims = JSON.parse(Buffer.from(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8'));
	if (!claims.federated_claims?.user_id) throw new Error('the token carries no federated_claims (the console asks Dex for federated:id)');
	return `dex~${claims.federated_claims.user_id}`;
}

/**
 * A person signed in to the console, in a browser context of their own.
 *
 * Returns the page, the latest bearer the console sent, the sign-in request's PKCE parameters, and the list of writes
 * the guard refused.
 */
export async function signIn(browser, email, { write, startPath = '/' }) {
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
	const session = { email, context, page: null, bearer: null, authorize: null, refused: [], apiCalls: [] };
	await context.route(`${ORIGIN}/api/**`, (route) => {
		const request = route.request();
		if (write || READ_METHODS.has(request.method())) return route.continue();
		session.refused.push(`${request.method()} ${new URL(request.url()).pathname}`);
		return route.abort('blockedbyclient');
	});
	const page = await context.newPage();
	session.page = page;
	page.on('request', async (request) => {
		const url = new URL(request.url());
		if (url.origin !== ORIGIN) return;
		if (url.pathname.startsWith('/dex/auth') && url.searchParams.has('client_id')) {
			session.authorize = {
				clientId: url.searchParams.get('client_id'),
				method: url.searchParams.get('code_challenge_method'),
				hasChallenge: url.searchParams.has('code_challenge'),
			};
		}
		if (!url.pathname.startsWith('/api/')) return;
		// `allHeaders`, not `headers`: the synchronous list leaves out security-related headers, this one among them.
		const authorization = (await request.allHeaders()).authorization;
		if (authorization?.startsWith('Bearer ')) session.bearer = authorization.slice('Bearer '.length);
	});
	page.on('response', (response) => {
		const url = new URL(response.url());
		if (url.origin === ORIGIN && url.pathname.startsWith('/api/')) {
			session.apiCalls.push(`${response.status()} ${response.request().method()} ${url.pathname}`);
		}
	});
	await page.goto(`${ORIGIN}${startPath}`, { waitUntil: 'domcontentloaded' });
	await completeSignIn(session);
	return session;
}

/** Finish a sign-in the console started: fill Dex's form when it asks, then wait to be back in the app with a bearer. */
async function completeSignIn(session) {
	const { page, email } = session;
	session.bearer = null;
	// The app paints at the requested path before it sends a signed-out visit to Dex, so the URL alone cannot say the
	// round trip is over: the first bearer the console sends can.
	const deadline = Date.now() + STEP_MS;
	let filled = false;
	while (!session.bearer && Date.now() < deadline) {
		const path = new URL(page.url()).pathname;
		if (path.startsWith('/dex/') && !filled) {
			const login = page.locator('input[name=login]');
			const chooser = page.locator('a, button').filter({ hasText: /email|password|log in/i });
			if ((await login.count()) === 0 && (await chooser.count()) > 0) await chooser.first().click();
			if ((await login.count()) > 0) {
				await login.fill(email);
				await page.fill('input[name=password]', PASSWORD);
				await page.click('button[type=submit]');
				filled = true;
			}
		}
		const approve = page.locator('button[type=submit]', { hasText: /grant|approve/i });
		if (path.startsWith('/dex/approval') && (await approve.count())) await approve.click();
		await page.waitForTimeout(250);
	}
	if (!session.bearer) {
		throw new Error(`${email}: no bearer reached /api/* within ${STEP_MS} ms; at ${new URL(page.url()).pathname}, api calls: ${session.apiCalls.join(', ') || 'none'}`);
	}
	await page.waitForURL((u) => u.origin === ORIGIN && !u.pathname.startsWith('/auth/') && !u.pathname.startsWith('/dex/'), { timeout: STEP_MS });
}

/** A full reload, which signs in again: the console holds its tokens in memory only. */
export async function reload(session, path = '/') {
	await session.page.goto(`${ORIGIN}${path}`, { waitUntil: 'domcontentloaded' });
	await completeSignIn(session);
}

/** An in-app move: a click on a link the console's router takes, so the session survives. A page that redirects
 *  within its own section (`/admin` to `/admin/events`) still counts as reached. */
export async function visit(session, path) {
	const { page } = session;
	await page.evaluate((p) => {
		const a = document.createElement('a');
		a.href = p;
		document.body.appendChild(a);
		a.click();
		a.remove();
	}, path);
	await page.waitForURL((u) => u.pathname === path || u.pathname.startsWith(`${path}/`), { timeout: STEP_MS });
	await page.waitForLoadState('networkidle').catch(() => {});
	await page.waitForTimeout(800);
}

/** The text of the page's main region, whitespace folded. */
export async function mainText(page) {
	const main = page.locator('main').first();
	const text = (await main.count()) ? await main.innerText() : await page.locator('body').innerText();
	return text.replace(/\s+/g, ' ').trim();
}

/** The bell's badge as a number; `0` when the badge is absent, which is how the component renders zero. */
export async function badge(page) {
	const slot = page.locator('[data-slot="notification-count"]').first();
	if ((await slot.count()) === 0) return 0;
	return Number(((await slot.textContent()) ?? '').trim().replace('+', ''));
}

/** Count the bells: the shell renders exactly one on every page. */
export async function bells(page) {
	return page.getByRole('button', { name: /^Notifications/ }).count();
}

/**
 * One call to the estate's own origin with the session's bearer (or none), under the write guard. Returns the status
 * and the parsed body; prints neither the token nor the body.
 */
export async function api(session, method, path, { body, write = false, anonymous = false } = {}) {
	if (!READ_METHODS.has(method) && !write) {
		if (session) session.refused.push(`${method} ${path}`);
		throw new Error(`refused ${method} ${path}: pass --write to run the writing steps`);
	}
	const headers = {};
	if (!anonymous) headers.authorization = `Bearer ${session.bearer}`;
	if (body !== undefined) headers['content-type'] = 'application/json';
	const res = await fetch(`${ORIGIN}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
	const text = await res.text().catch(() => '');
	let json = null;
	try {
		json = text ? JSON.parse(text) : null;
	} catch {
		json = null;
	}
	return { status: res.status, json, detail: json?.detail ?? (json ? '' : text.slice(0, 120)) };
}

/** Poll `read()` until `done(value)` or the deadline, returning the last value either way. */
export async function until(read, done, ms) {
	const deadline = Date.now() + ms;
	let value = await read();
	while (!done(value) && Date.now() < deadline) {
		await new Promise((r) => setTimeout(r, 3000));
		value = await read();
	}
	return value;
}
