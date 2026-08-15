/**
 * THE TOP GATE: alice's failed run increments ALICE's badge and not bob's.
 *
 * This is the one claim the notification plane exists to make, and the one no lower layer can
 * express. Unit tests prove `audience_for` returns the author; the visibility tests prove a
 * non-recipient is filtered. Neither can prove that two signed-in people, looking at the same estate
 * through the same shared component, see DIFFERENT numbers — which is the exact opposite of the
 * behaviour this plane replaced, where the badge counted everyone's work.
 *
 * WHY THIS FILE EXISTS AT ALL. The drive was performed once (recorded in `e315cb93`) and left no
 * harness: `scripts/verify_notifications.mjs` is the S1-era SINGLE-user script from the lance-ns
 * merge — it logs in as alice alone, asserts the bell renders from `/runs`, and writes screenshots to
 * a `lance-ns` path that no longer exists. So the plane's central claim was witnessed and then became
 * unreproducible. A claim nobody can re-run is a claim that silently rots.
 *
 * STATUS: AUTHORED, NOT YET DRIVEN. The cluster it targets does not currently host
 * `rask-notifications` (helm release rev 33 predates the service), so this has never been executed.
 * Do not read a green CI as evidence it passed — nothing runs it automatically. Say "driven" only
 * after you have watched the output.
 *
 * PREREQUISITES
 *   - `rask-notifications` deployed WITH a Dapr sidecar, and `notifications` present in
 *     `Component/lance-statestore`'s `scopes` (without it daprd disables actor hosting and every
 *     inbox route 503s — a healthy pod with a permanently empty bell).
 *   - Dex reachable, with the static users `alice` and `bob`.
 *   - The ingress (or a port-forward) on ORIGIN.
 *
 * WHY IT LIVES IN `tests/e2e` AND NOT `scripts/`. It imports `@playwright/test`, and ESM resolves a
 * bare specifier relative to the SCRIPT, not the working directory — so the same file under
 * `scripts/` cannot see the only install of it in this repo (`tests/e2e/node_modules`) no matter
 * where it is invoked from. `tests/e2e` is also where it belongs on its own terms: the standalone
 * Playwright project with its own lockfile, whose whole purpose is driving a RUNNING deploy.
 *
 * RUN
 *   kubectl port-forward -n kube-system svc/traefik 8080:80 &
 *   cd tests/e2e && node verify_notifications_two_users.mjs
 */
import { chromium } from '@playwright/test';

// 8080, not 8090, and NOT a separate Dex host — both are facts about this deployment, read from it
// rather than assumed: `rask-web-home` carries `OIDC_ISSUER=http://localhost:8080/dex` and
// `OIDC_REDIRECT_URI=http://localhost:8080/auth/callback`, and the ingress routes `/dex` to
// `rask-dex` alongside `/` to the home zone. So ONE origin serves the estate and its issuer, and the
// `--host-resolver-rules` mapping the older drives need (their Dex was a separate host the browser
// could not resolve) is unnecessary here and would silently misdirect the token exchange.
//
//   kubectl port-forward -n kube-system svc/traefik 8080:80
const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8080';
const SHOT = process.env.SHOT_DIR ?? '/tmp/rask-notifications-drive';
// Lineage is addressed DIRECTLY (see emitRun) — the gateway cannot carry a service token.
const LINEAGE_URL = process.env.LINEAGE_URL ?? 'http://localhost:8001';
const APP_TOKEN = process.env.APP_API_TOKEN ?? '';
const SERVICE_IDENTITY = process.env.LINEAGE_SERVICE_IDENTITY ?? 'service-web';
// The output the emitting identity must hold `can_write_data` on (model.fga: `can_write_data: writer`).
const OUTPUT = process.env.DRIVE_OUTPUT ?? 'bronze$events';
/** How long to let the bell's live query settle after a reload. */
const SETTLE_MS = Number(process.env.SETTLE_MS ?? 2500);
/** WALL-CLOCK bound on one wait, not an attempt count.
 *
 *  The first run of this drive exited 124 because the waits were `for (attempt < 10)` over TWO pages
 *  at 2.5 s each: when the badge never moves — the exact case a FAILED emit produces — that is ~50 s
 *  per phase and the whole run outgrew its timeout, so the harness reported a TIMEOUT where it had
 *  actually finished deciding. A failing assertion must cost about as much as a passing one. */
const WAIT_MS = Number(process.env.WAIT_MS ?? 45_000);

/** Poll `read()` until `done(value)` or the deadline. Returns the last value either way — this
 *  reports, it never throws, because "it did not move" IS the finding. */
async function until(read, done) {
	const deadline = Date.now() + WAIT_MS;
	let value = await read();
	while (!done(value) && Date.now() < deadline) value = await read();
	return value;
}

const browser = await chromium.launch();

let failures = 0;
const check = (label, ok, detail = '') => {
	console.log(`   ${ok ? '✓' : '✗'} ${label}${detail ? ` — ${detail}` : ''}`);
	if (!ok) failures += 1;
};

async function signIn(user, startPath = '/lakehouse/') {
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent(startPath)}`, { waitUntil: 'domcontentloaded' });
	await page.waitForURL(/\/dex\/auth/, { timeout: 20_000 });
	await page.fill('input[name="login"], input#login, input[type="text"], input[type="email"]', user);
	await page.fill('input[name="password"], input#password, input[type="password"]', 'password');
	await page.click('button[type="submit"], input[type="submit"], #submit-login');
	await page.waitForURL((u) => u.origin === ORIGIN && !u.pathname.startsWith('/auth'), { timeout: 25_000 });
	return { context, page };
}

/**
 * The badge, as a NUMBER, read off the shared component's own slot.
 *
 * `0` when the element is absent, because the component renders no badge at zero — that is the
 * distinction the whole drive turns on, so it must not be conflated with "could not read".
 */
async function badge(page) {
	await page.reload({ waitUntil: 'domcontentloaded' });
	// The bell opens a live query on mount; give it a beat to land rather than racing it.
	await page.waitForTimeout(SETTLE_MS);
	const text = await page.locator('[data-slot="notification-count"]').first().textContent().catch(() => null);
	return text === null ? 0 : Number(text.trim().replace('+', ''));
}

/**
 * Emit ONE terminal OpenLineage event authored by `subject`.
 *
 * Posted through lineage's own HTTP door rather than the bus on purpose: that is the lane the
 * reconciler exists for, so driving it exercises BOTH ingresses' convergence rather than only the
 * subscription. `enforce_author` overwrites whatever the body claims with the token's sub, which is
 * why the author cannot simply be asserted here — it is the door that decides.
 */
/**
 * Emit ONE terminal OpenLineage event, authored by `subject`, through lineage's SERVICE DOOR.
 *
 * NOT from the browser, and that is a fact about the estate rather than a convenience. A page-side
 * `fetch('/api/lineage/...')` carries no bearer: the session lives in a sealed httpOnly cookie scoped
 * to the ZONE, and the gateway does not translate cookie to bearer. Worse, going through the gateway
 * at all forecloses the service door — `is_public_caller` refuses a service token presented by a
 * public front door (the measured bypass), so the request falls through to OIDC and 401s. Measured
 * both ways here: `/api/lineage/api/v1/lineage` through the gateway answers 401 "Missing bearer
 * token", while the same body against lineage directly authenticates.
 *
 * So the drive talks to lineage on LINEAGE_URL with the pair the door wants.
 */
async function emitRun({ runId, state, authorSub, outputName = OUTPUT }) {
	const res = await fetch(`${LINEAGE_URL}/api/v1/lineage`, {
		method: 'POST',
		headers: {
			'content-type': 'application/json',
			'dapr-api-token': APP_TOKEN,
			'x-lance-service-identity': SERVICE_IDENTITY,
		},
		body: JSON.stringify({
			eventType: state,
			eventTime: new Date().toISOString(),
			producer: 'rask://verify_notifications_two_users',
			job: { namespace: 'rask-drive', name: `drive_${runId}` },
			run: { runId, facets: { author: { name: authorSub, sub: authorSub } } },
			outputs: [{ namespace: outputName.split('$')[0], name: outputName }],
		}),
	});
	return { status: res.status, body: await res.text().catch(() => '') };
}

console.log(`\n▸ two-user notification drive against ${ORIGIN}\n`);

const alice = await signIn('alice@example.com');
const bob = await signIn('bob@example.com');

const before = { alice: await badge(alice.page), bob: await badge(bob.page) };
console.log(`   baseline: alice=${before.alice} bob=${before.bob}`);

// ── alice's run FAILS ────────────────────────────────────────────────────────────────────────────
const runId = `drive-alice-${Date.now()}`;
const emitted = await emitRun({ runId, state: 'FAIL', authorSub: 'alice' });
check('lineage accepted the FAIL event', emitted.status < 400, `HTTP ${emitted.status} ${emitted.body.slice(0, 160)}`);

// The bus hop plus the fan-out is not instant; poll rather than sleep once and hope.
const after = await until(
	async () => ({ alice: await badge(alice.page), bob: await badge(bob.page) }),
	(v) => v.alice > before.alice,
);
console.log(`   after alice's FAIL: alice=${after.alice} bob=${after.bob}`);

check('alice was told about her own failed run', after.alice > before.alice, `${before.alice} → ${after.alice}`);
check("bob was NOT told about alice's run", after.bob === before.bob, `${before.bob} → ${after.bob}`);

await alice.page.screenshot({ path: `${SHOT}/alice-after-fail.png` }).catch(() => {});
await bob.page.screenshot({ path: `${SHOT}/bob-after-fail.png` }).catch(() => {});

// ── the reverse, because one direction proves only that SOMETHING moved ──────────────────────────
const bobRun = `drive-bob-${Date.now()}`;
await emitRun({ runId: bobRun, state: 'COMPLETE', authorSub: 'bob' });
const reverse = await until(
	async () => ({ alice: await badge(alice.page), bob: await badge(bob.page) }),
	(v) => v.bob > after.bob,
);
console.log(`   after bob's COMPLETE: alice=${reverse.alice} bob=${reverse.bob}`);
check('bob was told about his own completed run', reverse.bob > after.bob, `${after.bob} → ${reverse.bob}`);
check("alice was NOT told about bob's run", reverse.alice === after.alice, `${after.alice} → ${reverse.alice}`);

// ── B2: read state is DURABLE, not per-tab ───────────────────────────────────────────────────────
// The acceptance `OPEN-WORK.md` B2 named. Opening and closing the panel marks the rendered rows read;
// a FRESH BROWSER CONTEXT is what separates "persisted per subject" from "remembered in this tab".
const bell = alice.page.getByRole('button', { name: /notification/i }).first();
await bell.click().catch(() => {});
await alice.page.waitForTimeout(1200);
await alice.page.keyboard.press('Escape');
await alice.page.waitForTimeout(1500);

const fresh = await signIn('alice@example.com');
const freshBadge = await badge(fresh.page);
check('alice’s read state survived a FRESH browser context', freshBadge === 0, `fresh badge = ${freshBadge}`);

const bobStill = await badge(bob.page);
check('marking alice read did not touch bob', bobStill === reverse.bob, `bob = ${bobStill}`);

await browser.close();
console.log(`\n${failures === 0 ? '✅ all checks passed' : `❌ ${failures} check(s) failed`}\n`);
process.exit(failures === 0 ? 0 : 1);
