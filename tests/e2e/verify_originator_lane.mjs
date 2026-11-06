/**
 * The ORIGINATOR lane, driven against the deployed estate: a run whose author is not you still reaches you, because
 * it names you as the person the work was for.
 *
 * WHY THE ROLES ARE SPLIT THIS WAY. `enforce_author` (lineage/api/fga_deps.py) overwrites the author facet with the
 * caller's verified subject, so the run is posted as ALICE (its author, as the estate insists) and names BOB in
 * `rask_lance.originator`. Bob therefore receives a row for a run he did not author and does not watch, which no other
 * targeting source can produce. In production the author is a SERVICE writing on a person's behalf; a drive holding
 * only people's credentials cannot be one, and the originator field is the same either way.
 *
 *   ALICE  author     -> a row, reason `author`     (the control)
 *   BOB    originator -> a row, reason `originator` (the claim)
 *
 * TWO PHASES, AND ONLY THE FIRST RUNS BY DEFAULT.
 *   read (always) — both sign in to the console through Dex with its PKCE round trip, and each one's inbox and badge
 *     door answer their own bearer. Nothing is written.
 *   write (`--write --table <id>`) — alice posts ONE FAIL run on the table to lineage's OpenLineage door, naming bob as
 *     its originator. `--table` names a governed table alice may write and bob may read (delivery asks
 *     `can_be_notified` per recipient), ideally a scratch table, since the run is recorded in its lineage. The run
 *     reaches the inbox on its reconcile tick over lineage's feed (`@every 30s`).
 *
 * Prerequisites as for verify_notifications_two_users.mjs:
 *   kubectl -n kube-system port-forward svc/traefik 8080:80 &
 *   cd tests/e2e && bun verify_originator_lane.mjs
 */
import { randomUUID } from 'node:crypto';

import { ORIGIN, api, checker, launch, parseArgs, signIn, subjectOf, until } from './console.mjs';

const USAGE = `usage: bun verify_originator_lane.mjs [--write --table <namespace$table>]

  (no flags)  the read phase only: sign-in and GETs; nothing is written
  --write     also post one FAIL run as alice that names bob as its originator
  --table     a governed table alice may write and bob may read

env: ORIGIN (default http://localhost:8080), WAIT_MS (default 120000), DEX_PASSWORD`;

const args = parseArgs(process.argv.slice(2), USAGE);
const { table: TABLE } = args.values;
if (args.write && !TABLE) throw new Error(`--write needs --table\n\n${USAGE}`);
const WAIT_MS = Number(process.env.WAIT_MS ?? 120_000);

const checks = checker();
const { check } = checks;
const browser = await launch();
console.log(`\n> originator lane against ${ORIGIN} (${args.write ? 'read and write' : 'read only'})\n`);

const alice = await signIn(browser, 'alice@example.com', { write: args.write });
const bob = await signIn(browser, 'bob@example.com', { write: args.write });
const aliceSub = subjectOf(alice.bearer);
const bobSub = subjectOf(bob.bearer);
check('alice and bob are two subjects', aliceSub !== bobSub);

const unread = async (s) => api(s, 'GET', '/api/notifications/inbox/unread');
for (const [who, s] of [['alice', alice], ['bob', bob]]) {
	const badgeDoor = await unread(s);
	const inbox = await api(s, 'GET', '/api/notifications/inbox?state=unread&limit=100');
	check(`${who}'s inbox answers their bearer`, badgeDoor.status === 200 && inbox.status === 200, `GET /inbox/unread ${badgeDoor.status}, GET /inbox ${inbox.status}`);
}

if (args.write) {
	const counts = async () => ({ alice: (await unread(alice)).json?.unread, bob: (await unread(bob)).json?.unread });
	const before = await counts();
	console.log(`   before: unread alice=${before.alice} bob=${before.bob}`);
	const runId = randomUUID();
	const emitted = await api(alice, 'POST', '/api/lineage/api/v1/lineage', {
		write: true,
		body: {
			eventType: 'FAIL',
			eventTime: new Date().toISOString(),
			producer: 'rask://tests/e2e/verify_originator_lane',
			schemaURL: 'https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent',
			job: { namespace: 'rask-verify', name: 'originator' },
			// `originator` is the field under test, read by `originator_subject`: a targeting hint that authorizes
			// nothing, since delivery re-checks bob's own grant on the output.
			run: { runId, facets: { rask_lance: { originator: bobSub } } },
			outputs: [{ namespace: TABLE.slice(0, TABLE.lastIndexOf('$')), name: TABLE }],
		},
	});
	check('lineage recorded the run', emitted.status === 201, `POST /api/v1/lineage ${emitted.status} ${emitted.detail}`);
	const after = await until(counts, (c) => c.alice > before.alice && c.bob > before.bob, WAIT_MS);
	console.log(`   after:  unread alice=${after.alice} bob=${after.bob}`);
	check('alice was told as the AUTHOR', after.alice === before.alice + 1, `${before.alice} -> ${after.alice}`);
	check('bob was told as the ORIGINATOR', after.bob === before.bob + 1, `${before.bob} -> ${after.bob}`);
	const rows = (await api(bob, 'GET', '/api/notifications/inbox?limit=100')).json?.notifications ?? [];
	const row = rows.find((r) => r.notification_id.startsWith(`${runId}@`));
	check('bob’s row is for this run', Boolean(row), row ? '' : `saw ${rows.length} row(s)`);
	check('and its stored reason is `originator`, not `author`', row?.reason === 'originator', `reason=${row?.reason}`);
}

for (const [who, s] of [['alice', alice], ['bob', bob]]) {
	console.log(` ${who}: writes the guard refused: ${s.refused.length ? s.refused.join(', ') : 'none'}`);
	await s.context.close();
}
await browser.close();
console.log(`\n${checks.failures === 0 ? 'all checks passed' : `${checks.failures} check(s) failed`}\n`);
process.exit(checks.failures === 0 ? 0 : 1);
