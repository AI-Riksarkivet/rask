/**
 * Two people, one console: each is told about their own work and not the other's, and the console tells them apart.
 *
 * The claim the notification plane exists to make, and the one no lower layer can express: two signed-in people,
 * looking at the same estate through the same bell, see DIFFERENT numbers. Unit tests prove `audience_for` returns the
 * author and the visibility tests prove a non-recipient is filtered; neither proves the deployed estate keeps two
 * people's inboxes apart.
 *
 * TWO PHASES, AND ONLY THE FIRST RUNS BY DEFAULT.
 *
 *   read (always) — alice (an estate admin) and bob (not one) each sign in to the console in a browser of their own,
 *     through Dex, with the console's own PKCE round trip on its public client. Per person: the sign-in used
 *     `rask-console` with an S256 challenge; the catalog's `/me` names them, and only alice as an estate admin; every
 *     page of the console carries one bell; `/admin` opens for alice and refuses bob, and the catalog's own
 *     estate-admin door answers alice 200 and bob 403, so the difference is the server's and not only the client
 *     guard's; the inbox, its badge door, the watch list and the preferences answer the person's bearer; the bell's
 *     badge agrees with the inbox; and the inbox refuses a request with no bearer. Nothing in this phase writes: the
 *     browser aborts every non-GET to `/api/*` and the run reports each one it refused.
 *
 *   write (`--write --table <id> --project <id>`) — the targeting itself, which needs writes:
 *     W1 alice posts a FAIL run on the table to lineage's OpenLineage door: alice is told, bob is not (AUTHOR).
 *     W2 bob posts a COMPLETE run on it: bob is told, alice is not.
 *     W3 alice watches the project through the inbox's watch door (`project#member`-gated).
 *     W4 bob posts a FAIL run carrying the project: alice is told as a WATCHER, bob as its author.
 *     W5 alice opens her bell, which marks what it rendered as seen; a FRESH browser context reads the lower count,
 *        so read state is the inbox's and not the tab's, and bob's count is untouched. The panel is asserted inside
 *        its own dialog, and every text row in it is measured for silent clipping.
 *     W6 alice unwatches the project, unless she watched it before the run.
 *
 * THE RUNS ARE EMITTED AS THE PEOPLE, with the bearer the console holds. `enforce_author` overwrites the author facet
 * with the verified caller, so a run cannot be attributed by its body; lineage resolves the output's name to the
 * table's uuid as the caller sees it, and refuses a caller without `can_write_data` on it. `--table` therefore names
 * a governed table both people may write, ideally a scratch table, since each run is recorded in its lineage. The
 * runs reach the inbox on its reconcile tick over lineage's feed (`@every 30s`), not on the bus: lineage's HTTP door
 * records and publishes nothing.
 *
 * Prerequisites: the lakehouse and the console's release deployed (`make k3s-up`, `make k3s-clients`); the dev Dex
 * users alice and bob; the Gateway on ORIGIN:
 *   kubectl -n kube-system port-forward svc/traefik 8080:80 &
 *   cd tests/e2e && bun verify_notifications_two_users.mjs
 */
import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';

import { ORIGIN, api, badge, bells, checker, launch, mainText, parseArgs, reload, signIn, subjectOf, until, visit } from './console.mjs';

const USAGE = `usage: bun verify_notifications_two_users.mjs [--write --table <namespace$table> --project <project id>]

  (no flags)  the read phase only: sign-in, GETs and page visits; nothing is written
  --write     also run W1-W6, which post three lineage runs, watch and unwatch a project, and mark alice's rows seen
  --table     a governed table alice and bob may both write (lineage resolves it as each of them)
  --project   a project alice is a member of and bob does not watch

env: ORIGIN (default http://localhost:8080), SHOT_DIR, WAIT_MS (default 120000), DEX_PASSWORD`;

const args = parseArgs(process.argv.slice(2), USAGE);
const { table: TABLE, project: PROJECT } = args.values;
if (args.write && (!TABLE || !PROJECT)) throw new Error(`--write needs --table and --project\n\n${USAGE}`);
const SHOT = process.env.SHOT_DIR ?? '/tmp/rask-notifications-drive';
mkdirSync(SHOT, { recursive: true });
/** Above two reconcile ticks plus a reload's sign-in, so a run that is delivered is seen before the deadline. */
const WAIT_MS = Number(process.env.WAIT_MS ?? 120_000);
/** The console's pages a person reaches from its navbars; each renders the shell and its one bell. */
const PAGES = ['/', '/projects', '/catalog', '/catalog/tables', '/notifications', '/preferences', '/workbench'];
const PEOPLE = [
	{ who: 'alice', email: 'alice@example.com', admin: true },
	{ who: 'bob', email: 'bob@example.com', admin: false },
];

const checks = checker();
const { check } = checks;
const browser = await launch();
console.log(`\n> two people, one console, against ${ORIGIN} (${args.write ? 'read and write' : 'read only'})\n`);

// ── read ───────────────────────────────────────────────────────────────────────────────────────────────────────────
const sessions = {};
for (const person of PEOPLE) {
	console.log(` ${person.who}`);
	const s = await signIn(browser, person.email, { write: args.write });
	sessions[person.who] = s;
	s.subject = subjectOf(s.bearer);
	check('signed in on the console’s public client', s.authorize?.clientId === 'rask-console', `client_id=${s.authorize?.clientId}`);
	check('with a PKCE S256 challenge', s.authorize?.method === 'S256' && s.authorize.hasChallenge, `code_challenge_method=${s.authorize?.method}`);

	const me = await api(s, 'GET', '/api/catalog/management/v1/me');
	check('the catalog names them', me.status === 200 && me.json?.email === person.email && me.json?.sub === s.subject, `${me.status} ${me.json?.email}`);
	check(`estate admin: ${person.admin}`, me.json?.estate_admin === person.admin, `estate_admin=${me.json?.estate_admin}`);

	for (const path of PAGES) {
		await visit(s, path);
		const n = await bells(s.page);
		check(`${path} carries one bell`, n === 1, `${n} bell(s)`);
	}
	await visit(s, '/admin');
	s.adminText = await mainText(s.page);
	await s.page.screenshot({ path: `${SHOT}/${person.who}-admin.png` });
	const adminDoor = await api(s, 'GET', '/api/catalog/management/v1/events');
	s.adminStatus = adminDoor.status;
	if (person.admin) {
		check('/admin opens', /Events/.test(s.adminText) && !/estate-admin only/.test(s.adminText), s.adminText.slice(0, 80));
		check('the catalog’s estate-admin door answers', adminDoor.status === 200, `GET /management/v1/events ${adminDoor.status}`);
	} else {
		check('/admin refuses, saying why', /Admin is estate-admin only/.test(s.adminText), s.adminText.slice(0, 80));
		check('the catalog’s estate-admin door refuses', adminDoor.status === 403, `GET /management/v1/events ${adminDoor.status}`);
	}

	const inbox = await api(s, 'GET', '/api/notifications/inbox?limit=100');
	const unread = await api(s, 'GET', '/api/notifications/inbox/unread');
	const watches = await api(s, 'GET', '/api/notifications/watches');
	const prefs = await api(s, 'GET', '/api/notifications/prefs');
	check('the inbox answers their bearer', inbox.status === 200, `GET /inbox ${inbox.status}, ${inbox.json?.notifications?.length ?? '?'} row(s)`);
	check('the badge door answers, and agrees with the inbox', unread.status === 200 && unread.json?.unread === inbox.json?.unread, `GET /inbox/unread ${unread.status} unread=${unread.json?.unread}`);
	check('the watch list and preferences answer', watches.status === 200 && prefs.status === 200, `GET /watches ${watches.status}, GET /prefs ${prefs.status}`);
	s.unread = unread.json?.unread;
	s.watching = (watches.json?.projects ?? []).map((w) => (typeof w === 'string' ? w : w.project_id));

	const bellReads = s.apiCalls.filter((c) => c.endsWith(' GET /api/notifications/inbox'));
	check('the bell read the inbox with their bearer', bellReads.some((c) => c.startsWith('200 ')), bellReads.join(', ') || 'no read');
	await reload(s, '/notifications');
	await s.page.waitForTimeout(2500);
	const shown = await badge(s.page);
	check('the bell’s badge is the inbox’s unread count', shown === s.unread, `badge ${shown}, inbox ${s.unread}`);
	console.log('');
}
check('alice and bob are two subjects', sessions.alice.subject !== sessions.bob.subject);
check('alice and bob are told apart on /admin', sessions.alice.adminText !== sessions.bob.adminText);
const anonymous = await api(null, 'GET', '/api/notifications/inbox/unread', { anonymous: true });
check('the inbox refuses a request with no bearer', anonymous.status === 401, `GET /inbox/unread ${anonymous.status}`);

// ── write ──────────────────────────────────────────────────────────────────────────────────────────────────────────
if (args.write) {
	const { alice, bob } = sessions;
	const namespace = TABLE.slice(0, TABLE.lastIndexOf('$'));
	const counts = async () => ({
		alice: (await api(alice, 'GET', '/api/notifications/inbox/unread')).json?.unread,
		bob: (await api(bob, 'GET', '/api/notifications/inbox/unread')).json?.unread,
	});
	/** One terminal run, posted as `session`'s person. No author facet: `enforce_author` stamps the caller. */
	const emit = (session, state, { runId = randomUUID(), project } = {}) =>
		api(session, 'POST', '/api/lineage/api/v1/lineage', {
			write: true,
			body: {
				eventType: state,
				eventTime: new Date().toISOString(),
				producer: 'rask://tests/e2e/verify_notifications_two_users',
				schemaURL: 'https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent',
				job: { namespace: 'rask-verify', name: project ? 'two_users_watched' : 'two_users' },
				// The tenant rides the `rask_lance` run facet, which is what WATCH targets on; the authorship runs carry
				// none, so they reach their author alone.
				run: { runId, facets: project ? { rask_lance: { project } } : {} },
				outputs: [{ namespace, name: TABLE }],
			},
		});
	/** `session`'s inbox row for `runId`, read off the inbox door (ids are `<runId>@<STATE>`). */
	const rowFor = async (session, runId) => {
		const page = await api(session, 'GET', '/api/notifications/inbox?limit=100');
		return (page.json?.notifications ?? []).find((r) => r.notification_id.startsWith(`${runId}@`));
	};
	check('bob does not already watch the project', !bob.watching.includes(PROJECT), `bob watches ${bob.watching.join(', ') || 'nothing'}`);
	const before = await counts();
	console.log(` write phase: unread alice=${before.alice} bob=${before.bob}`);

	const w1 = await emit(alice, 'FAIL');
	check('W1 lineage recorded alice’s FAIL run', w1.status === 201, `POST /api/v1/lineage ${w1.status} ${w1.detail}`);
	const afterW1 = await until(counts, (c) => c.alice > before.alice, WAIT_MS);
	check('W1 alice was told about her own run', afterW1.alice === before.alice + 1, `${before.alice} -> ${afterW1.alice}`);
	check('W1 bob was not', afterW1.bob === before.bob, `${before.bob} -> ${afterW1.bob}`);

	const w2 = await emit(bob, 'COMPLETE');
	check('W2 lineage recorded bob’s COMPLETE run', w2.status === 201, `POST /api/v1/lineage ${w2.status} ${w2.detail}`);
	const afterW2 = await until(counts, (c) => c.bob > afterW1.bob, WAIT_MS);
	check('W2 bob was told about his own run', afterW2.bob === afterW1.bob + 1, `${afterW1.bob} -> ${afterW2.bob}`);
	check('W2 alice was not', afterW2.alice === afterW1.alice, `${afterW1.alice} -> ${afterW2.alice}`);

	const watchedBefore = alice.watching.includes(PROJECT);
	if (watchedBefore) console.log('   W3 skipped: alice already watches the project');
	else {
		const w3 = await api(alice, 'PUT', `/api/notifications/watches/${encodeURIComponent(PROJECT)}`, { write: true });
		check('W3 alice watches the project (the member gate admits her)', w3.status === 200, `PUT /watches/${PROJECT} ${w3.status} ${w3.detail}`);
	}
	const runW4 = randomUUID();
	const w4 = await emit(bob, 'FAIL', { runId: runW4, project: PROJECT });
	check('W4 lineage recorded bob’s run in the watched project', w4.status === 201, `POST /api/v1/lineage ${w4.status} ${w4.detail}`);
	const afterW4 = await until(counts, (c) => c.alice > afterW2.alice && c.bob > afterW2.bob, WAIT_MS);
	check('W4 alice was told, though she did not author it', afterW4.alice === afterW2.alice + 1, `${afterW2.alice} -> ${afterW4.alice}`);
	check('W4 bob was told as its author', afterW4.bob === afterW2.bob + 1, `${afterW2.bob} -> ${afterW4.bob}`);
	const aliceRow = await rowFor(alice, runW4);
	const bobRow = await rowFor(bob, runW4);
	check('W4 alice’s row says why: watch', aliceRow?.reason === 'watch', `reason=${aliceRow?.reason}`);
	check('W4 bob’s row says why: author', bobRow?.reason === 'author', `reason=${bobRow?.reason}`);

	// W5: the bell's panel, opened by alice in her browser.
	await reload(alice, '/notifications');
	await alice.page.waitForTimeout(2500);
	const openedWith = (await counts()).alice;
	await alice.page.getByRole('button', { name: /^Notifications/ }).first().click();
	await alice.page.waitForTimeout(1500);
	const panel = alice.page.getByRole('dialog', { name: 'Notifications' });
	check('W5 the bell opens a panel with its own role and name', (await panel.count()) === 1);
	const panelText = ((await panel.innerText().catch(() => '')) ?? '').replace(/\s+/g, ' ');
	check('W5 the panel, not the page, names the run alice was told about as a watcher', panelText.length > 0, panelText.slice(0, 80));
	const clipped = await panel.locator('p, span').filter({ hasText: /\S/ }).evaluateAll((els) =>
		els.slice(0, 20).flatMap((el) => {
			const cs = getComputedStyle(el);
			const overflows = el.clientHeight < el.scrollHeight || el.clientWidth < el.scrollWidth;
			const marked = (cs.webkitLineClamp !== 'none' && cs.webkitLineClamp !== '') || cs.textOverflow === 'ellipsis';
			if (!overflows) return [];
			if (!marked) return [`cut without a marker: ${(el.textContent ?? '').trim().slice(0, 40)}`];
			return el.getAttribute('title') ? [] : [`truncated with no title=: ${(el.textContent ?? '').trim().slice(0, 40)}`];
		}),
	);
	check('W5 no panel row is cut silently or truncated beyond reach', clipped.length === 0, clipped.join('; '));
	await panel.screenshot({ path: `${SHOT}/alice-panel.png` }).catch(() => {});
	await alice.page.keyboard.press('Escape');
	await alice.page.waitForTimeout(1500);
	const seenCalls = alice.apiCalls.filter((c) => c.endsWith(' POST /api/notifications/inbox/seen'));
	check('W5 the panel marked what it rendered as seen', seenCalls.some((c) => c.startsWith('200 ')), seenCalls.join(', ') || 'no POST /inbox/seen');

	const fresh = await signIn(browser, 'alice@example.com', { write: true, startPath: '/notifications' });
	await fresh.page.waitForTimeout(2500);
	const freshUnread = (await api(fresh, 'GET', '/api/notifications/inbox/unread')).json?.unread;
	const freshBadge = await badge(fresh.page);
	check('W5 a fresh browser reads the lower count: read state is the inbox’s', freshUnread < openedWith, `${openedWith} -> ${freshUnread}`);
	check('W5 and its badge shows it', freshBadge === freshUnread, `badge ${freshBadge}`);
	const bobAfter = (await counts()).bob;
	check('W5 alice’s reading did not touch bob', bobAfter === afterW4.bob, `${afterW4.bob} -> ${bobAfter}`);
	await fresh.context.close();

	if (!watchedBefore) {
		const w6 = await api(alice, 'DELETE', `/api/notifications/watches/${encodeURIComponent(PROJECT)}`, { write: true });
		check('W6 alice unwatches the project again', w6.status === 200, `DELETE /watches/${PROJECT} ${w6.status} ${w6.detail}`);
	}
}

for (const [who, s] of Object.entries(sessions)) {
	console.log(` ${who}: writes the guard refused: ${s.refused.length ? s.refused.join(', ') : 'none'}`);
	await s.context.close();
}
await browser.close();
console.log(`\n${checks.failures === 0 ? 'all checks passed' : `${checks.failures} check(s) failed`}\n`);
process.exit(checks.failures === 0 ? 0 : 1);
