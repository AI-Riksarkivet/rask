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
 * STATUS: DRIVEN GREEN against local k3s on 2026-08-15 — all six checks, exit 0. Nothing runs it
 * automatically, so a green CI is still not evidence it passed: say "driven" only after you have
 * watched the output yourself.
 *
 * FOUR DEPLOYMENT FACTS IT FOUND, none of which any unit test could have:
 *   1. `notifications` must hold FGA grants of its OWN. The feed is governed, so a deployment that
 *      forgets them gets a reconciler that ticks cleanly, logs `lineage_feed_reconciled`, and
 *      reconciles nothing — measured here as `GET /events` returning 0 rows for `notifications`
 *      while `service-ingest` saw 80.
 *   2. Its `*_OIDC_ISSUER` must be the PUBLIC issuer (`http://localhost:8080/dex`) with discovery
 *      pointed in-cluster — the split-horizon every other governed service uses. Both set to the
 *      in-cluster URL yields `OIDC discovery issuer mismatch` and a permanently 401'd inbox.
 *   3. The output table needs a real grant. `table:bronze$events` had zero tuples, so every emit
 *      403'd on `can_write_data` — and an orphaned table (no `parent` edge) is reachable ONLY by a
 *      direct grant, since `reader from parent` has no parent to walk.
 *   4. The first reconciler tick primes the cursor and notifies nobody, by design. A run emitted
 *      before that tick is never delivered, which looks exactly like a broken plane.
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
// Lineage is addressed DIRECTLY (see emitRun) — the gateway fronts it, but the door this drive needs
// is the ingest door and nothing here depends on the gateway's routing.
const LINEAGE_URL = process.env.LINEAGE_URL ?? 'http://localhost:8001';
// THE RUN MUST BE EMITTED AS THE USER, NOT AS A SERVICE, AND THAT IS NOT A DETAIL OF THIS HARNESS.
//
// The first version of this drive posted with `dapr-api-token` + `x-lance-service-identity:
// service-web` and put the intended author in the body as `run.facets.author.sub = 'alice'`. Lineage
// accepted it — HTTP 201, every time — and alice's badge never moved, which reads exactly like a
// broken notification plane. It is the opposite: `enforce_author` (services/lineage/api/fga_deps.py)
// OVERWRITES the claimed facet with the verified token subject, precisely so a producer cannot put a
// row in a named person's inbox. So the events landed authored by `service-web`, were delivered to
// `service-web`, and the drive spent three runs measuring a forgery guard doing its job.
//
// Two consequences, both load-bearing:
//   · The emitter must hold the USER's own token. Dex is `enablePasswordDB: true` with
//     `passwordConnector: local`, so the password grant mints one — the same mechanism the annotator's
//     publish saga already uses (services/annotator/projects/lakehouse.py), not a test-only backdoor.
//   · The author subject is DEX's `sub` (`CiQwOGE4…`), never the string `alice`. That is also the
//     InboxActor's id, which is why the two must come from one place: the token.
// THE DRIVE SEEDS ITS OWN GRANTS, and that is the difference between a test and an anecdote.
//
// The first green run of this drive was green partly because three tuples had been written into the
// cluster BY HAND that afternoon. They lived in no file, so the run was not reproducible: a fresh
// estate would have failed it, and the next person would have re-diagnosed a plane that was working.
// The drive's own header already warns that "a claim nobody can re-run is a claim that silently rots"
// — it was true of the drive itself.
//
// The chart's bootstrap grant does NOT cover this. It grants the notifications principal `reader` on
// `warehouse:lance_catalog`, which reaches everything BENEATH that warehouse — and the drive's output
// is `table:bronze$events`, an orphan with no `parent` edge, so `reader from parent` has nothing to
// walk. A synthetic dataset a producer names in a lineage event is not a catalog table and never gets
// one. Hence a direct grant, written here, every run.
//
// Roles, never `can_*`: `can_write_data` and `can_be_notified` are derived relations and OpenFGA
// refuses a direct assignment to them. Granting `writer`/`reader` is what makes the derived checks
// answer true, and it is what the estate's own model prescribes.
// `/api` on the SAME origin: the ingress routes it to rask-gateway:8888, which path-routes
// `/api/notifications` onward. Not a separate host — the zone's own bell reaches the inbox this way,
// so driving the watch door here exercises the path a browser actually uses.
const GATEWAY_URL = process.env.GATEWAY_URL ?? ORIGIN;
const FGA_API_URL = process.env.FGA_API_URL ?? 'http://localhost:18099';
const FGA_STORE_NAME = process.env.FGA_STORE_NAME ?? 'lance-catalog';
/** The notifications service's OWN principal — the reconciler reads lineage's governed feed as this,
 *  so it needs its own grant. Matches `RASK_LINEAGE_SERVICE_IDENTITY` / the chart's default. */
const SERVICE_SUBJECT = process.env.NOTIFICATIONS_SUBJECT ?? 'notifications';
/** The project used to prove WATCH targeting — created as tuples only; no catalog row is needed. */
const WATCH_PROJECT = process.env.WATCH_PROJECT ?? 'drive-watch-project';
const TOKEN_URL = process.env.TOKEN_URL ?? `${ORIGIN}/dex/token`;
const CLIENT_ID = process.env.OIDC_CLIENT_ID ?? 'lance-catalog';
const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? 'lance-catalog-secret';
const PASSWORD = process.env.DEX_PASSWORD ?? 'password';
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
/** Mint `email`'s own OIDC token from Dex via the password grant.
 *
 *  `id_token` first, `access_token` as the fallback — the same order and the same request shape the
 *  annotator's publish identity uses, so this exercises a path the estate already depends on. */
async function mintToken(email) {
	const body = new URLSearchParams({
		grant_type: 'password',
		username: email,
		password: PASSWORD,
		scope: 'openid profile email',
	});
	const res = await fetch(TOKEN_URL, {
		method: 'POST',
		headers: {
			'content-type': 'application/x-www-form-urlencoded',
			authorization: `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString('base64')}`,
		},
		body,
	});
	if (!res.ok) throw new Error(`dex refused ${email}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
	const json = await res.json();
	const token = json.id_token ?? json.access_token;
	if (!token) throw new Error(`dex returned neither id_token nor access_token for ${email}`);
	return token;
}

/** The `sub` inside a Dex token — the ONLY correct spelling of a person here.
 *
 *  Dex's subject is an opaque `CiQwOGE4…` string, not "alice". It is simultaneously the FGA principal
 *  (`user:<sub>`), the InboxActor's id, and what `enforce_author` stamps on the run — so all three
 *  must come from one place, and that place is the token. An earlier version of this drive passed the
 *  literal string 'alice' and addressed an inbox nobody owns. */
function subjectOf(token) {
	const part = token.split('.')[1];
	const json = Buffer.from(part.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
	const sub = JSON.parse(json).sub;
	if (!sub) throw new Error('token carries no sub');
	return sub;
}

/** Resolve the estate's store by NAME, newest-wins — the same tie-break `fga.provision` uses.
 *
 *  Paginated deliberately: a single-page read is how this session concluded a store was "phantom"
 *  when the listing simply had not been walked (it was in fact a stale port-forward, but the
 *  single-page read is what made the wrong answer look complete). */
async function fgaStoreId() {
	let token;
	do {
		const url = `${FGA_API_URL}/stores?page_size=50${token ? `&continuation_token=${token}` : ''}`;
		const res = await fetch(url);
		if (!res.ok) throw new Error(`openfga /stores: HTTP ${res.status}`);
		const body = await res.json();
		const hit = (body.stores ?? []).filter((s) => s.name === FGA_STORE_NAME);
		if (hit.length) return hit.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at))).pop().id;
		token = body.continuation_token || null;
	} while (token);
	throw new Error(`no OpenFGA store named ${FGA_STORE_NAME} at ${FGA_API_URL}`);
}

/** Write `tuples` idempotently. A duplicate is success — the estate may already be seeded, and a
 *  drive that fails on "already granted" would be unrunnable twice in a row. */
async function seedGrants(storeId, tuples) {
	for (const key of tuples) {
		const res = await fetch(`${FGA_API_URL}/stores/${storeId}/write`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ writes: { tuple_keys: [key] } }),
		});
		if (res.ok) continue;
		const text = await res.text();
		if (res.status === 400 && text.includes('already exists')) continue;
		throw new Error(`grant ${key.user} ${key.relation} ${key.object} failed: HTTP ${res.status} ${text.slice(0, 200)}`);
	}
}

/** Emit one run event AS the token's holder. The author facet is deliberately NOT sent: whatever it
 *  said would be overwritten by `enforce_author`, and sending one invites the reader to believe it
 *  had an effect. */
async function emitRun({ runId, state, token, outputName = OUTPUT, project }) {
	const res = await fetch(`${LINEAGE_URL}/api/v1/lineage`, {
		method: 'POST',
		headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
		body: JSON.stringify({
			eventType: state,
			eventTime: new Date().toISOString(),
			producer: 'rask://verify_notifications_two_users',
			job: { namespace: 'rask-drive', name: `drive_${runId}` },
			// The TENANT rides the `lance` run facet — the same bag `run_id` uses, and what
			// `project_id()` reads to find a run's watchers. Omitted entirely for the authorship
			// cases, so those keep proving v1's behaviour rather than quietly exercising v2.
			run: project ? { runId, facets: { lance: { project } } } : { runId },
			outputs: [{ namespace: outputName.split('$')[0], name: outputName }],
		}),
	});
	return { status: res.status, body: await res.text().catch(() => '') };
}

/** Register a watch AS the subject, through the estate's own door.
 *
 *  Deliberately the real HTTP route rather than a tuple write: the door is `project#member`-gated,
 *  so driving it proves the gate admits a member — a seeded actor-state row would prove nothing
 *  about authorization and would skip the very check v2 rests on. */
async function watchProject(token, project) {
	const res = await fetch(`${GATEWAY_URL}/api/notifications/watches/${encodeURIComponent(project)}`, {
		method: 'PUT',
		headers: { authorization: `Bearer ${token}` },
	});
	return { status: res.status, body: await res.text().catch(() => '') };
}

console.log(`\n▸ two-user notification drive against ${ORIGIN}\n`);

const alice = await signIn('alice@example.com');
const bob = await signIn('bob@example.com');

// Their OWN tokens, minted from the same IdP the browser session came from — see the emitRun note.
const aliceToken = await mintToken('alice@example.com');
const bobToken = await mintToken('bob@example.com');
const aliceSub = subjectOf(aliceToken);
const bobSub = subjectOf(bobToken);

// ── seed, every run ──────────────────────────────────────────────────────────────────────────────
// Four grants, each present because a specific check cannot pass without it:
//   · alice/bob `writer` on the output — `enforce_output_authz` refuses the emit otherwise (403).
//   · notifications `reader` on the output — the feed is GOVERNED, so without it the reconciler
//     walks a page it cannot see and delivers nothing, cleanly and silently.
//   · alice `member` of the watch project — the watch door is `project#member`-gated.
const storeId = await fgaStoreId();
await seedGrants(storeId, [
	{ user: `user:${aliceSub}`, relation: 'writer', object: `table:${OUTPUT}` },
	{ user: `user:${bobSub}`, relation: 'writer', object: `table:${OUTPUT}` },
	{ user: `user:${SERVICE_SUBJECT}`, relation: 'reader', object: `table:${OUTPUT}` },
	{ user: `user:${aliceSub}`, relation: 'member', object: `project:${WATCH_PROJECT}` },
]);
console.log(`   seeded 4 grants in store ${storeId}`);

const before = { alice: await badge(alice.page), bob: await badge(bob.page) };
console.log(`   baseline: alice=${before.alice} bob=${before.bob}`);

// ── alice's run FAILS ────────────────────────────────────────────────────────────────────────────
const runId = `drive-alice-${Date.now()}`;
const emitted = await emitRun({ runId, state: 'FAIL', token: aliceToken });
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
await emitRun({ runId: bobRun, state: 'COMPLETE', token: bobToken });
const reverse = await until(
	async () => ({ alice: await badge(alice.page), bob: await badge(bob.page) }),
	(v) => v.bob > after.bob,
);
console.log(`   after bob's COMPLETE: alice=${reverse.alice} bob=${reverse.bob}`);
check('bob was told about his own completed run', reverse.bob > after.bob, `${after.bob} → ${reverse.bob}`);
check("alice was NOT told about bob's run", reverse.alice === after.alice, `${after.alice} → ${reverse.alice}`);

// ── v2 targeting: a WATCHER is told about someone else's run ─────────────────────────────────────
// The second of the plane's targeting sources, and until now the untested one. Everything above
// proves authorship, which needs no registry and no permission — you may always be told about your
// own run. A watch is the opposite shape: an explicit, `project#member`-gated opt-in that puts a run
// in the inbox of somebody who did NOT run it. Nothing in the authorship path exercises the watch
// registry, the membership gate, or the WATCH delivery reason.
const watched = await watchProject(aliceToken, WATCH_PROJECT);
check('alice could register a watch (project#member gate admits a member)', watched.status < 400, `HTTP ${watched.status} ${watched.body.slice(0, 120)}`);

const beforeWatch = { alice: await badge(alice.page), bob: await badge(bob.page) };
const watchRun = `drive-watch-${Date.now()}`;
// BOB runs it, carrying the project alice watches. Alice is not the author, so if her badge moves it
// moved for exactly one reason.
const watchEmit = await emitRun({ runId: watchRun, state: 'FAIL', token: bobToken, project: WATCH_PROJECT });
check('lineage accepted the watched-project run', watchEmit.status < 400, `HTTP ${watchEmit.status} ${watchEmit.body.slice(0, 120)}`);

const afterWatch = await until(
	async () => ({ alice: await badge(alice.page), bob: await badge(bob.page) }),
	(v) => v.alice > beforeWatch.alice,
);
console.log(`   after bob's run in alice's watched project: alice=${afterWatch.alice} bob=${afterWatch.bob}`);
check("alice was told about a run she did NOT author, because she watches its project", afterWatch.alice > beforeWatch.alice, `${beforeWatch.alice} → ${afterWatch.alice}`);
check('bob was still told too — he authored it', afterWatch.bob > beforeWatch.bob, `${beforeWatch.bob} → ${afterWatch.bob}`);

await alice.page.screenshot({ path: `${SHOT}/alice-after-watch.png` }).catch(() => {});

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

// Against `afterWatch`, not `reverse`: the watch section moved bob's badge too (he authored that
// run), so comparing to the pre-watch figure would fail for a reason that has nothing to do with
// read state. The claim is "alice's read did not change BOB's count", so the baseline has to be
// bob's count at the moment alice read.
const bobStill = await badge(bob.page);
check('marking alice read did not touch bob', bobStill === afterWatch.bob, `bob = ${bobStill}, expected ${afterWatch.bob}`);

await browser.close();
console.log(`\n${failures === 0 ? '✅ all checks passed' : `❌ ${failures} check(s) failed`}\n`);
process.exit(failures === 0 ? 0 : 1);
