/** The ORIGINATOR lane, driven against the deployed estate.
 *
 * The claim under test is the one no unit test can make: that a run whose AUTHOR is not you still
 * reaches you, on a real Dapr actor plane, because you are named as the human the work is for.
 *
 * WHY THE ROLES ARE SPLIT THIS WAY. `enforce_author` (lineage/api/fga_deps.py) overwrites the author
 * facet with the CALLER's verified sub — that is the whole point of the HTTP door, and it is why a
 * producer cannot self-assert someone else's identity. So the drive posts as ALICE (making her the
 * author, exactly as the estate insists) while naming BOB in `lance.originator`. Bob therefore
 * receives a row for a run he did not author and does not watch, which is possible under no other
 * targeting source in the plane. In production the same shape occurs with a SERVICE as the author —
 * a medallion mover authoring as `data_eng` — and that case is unreachable from a drive holding only
 * human credentials.
 *
 *   ALICE  author     -> a row, reason `author`     (v1, unchanged — the control)
 *   BOB    originator -> a row, reason `originator` (v5, the thing being proven)
 *
 * Run:  ORIGIN=http://localhost:8080 node tests/e2e/verify_originator_lane.mjs
 */

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8080';
const LINEAGE_URL = process.env.LINEAGE_URL ?? `${ORIGIN}/api/lineage`;
const GATEWAY_URL = process.env.GATEWAY_URL ?? ORIGIN;
const FGA_API_URL = process.env.FGA_API_URL ?? 'http://localhost:18099';
const FGA_STORE_NAME = process.env.FGA_STORE_NAME ?? 'lance-catalog';
const TOKEN_URL = process.env.TOKEN_URL ?? `${ORIGIN}/dex/token`;
const CLIENT_ID = process.env.OIDC_CLIENT_ID ?? 'lance-catalog';
const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? 'lance-catalog-secret';
const PASSWORD = process.env.DEX_PASSWORD ?? 'password';
const OUTPUT = process.env.DRIVE_OUTPUT ?? 'bronze$events';
const SETTLE_MS = Number(process.env.SETTLE_MS ?? 4000);

let failures = 0;
const check = (label, ok, detail = '') => {
	console.log(`${ok ? '  ✔' : '  ✘'} ${label}${detail ? ` — ${detail}` : ''}`);
	if (!ok) failures += 1;
};

async function mintToken(email) {
	const res = await fetch(TOKEN_URL, {
		method: 'POST',
		headers: {
			'content-type': 'application/x-www-form-urlencoded',
			authorization: `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString('base64')}`,
		},
		body: new URLSearchParams({ grant_type: 'password', username: email, password: PASSWORD, scope: 'openid profile email' }),
	});
	if (!res.ok) throw new Error(`dex refused ${email}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
	const json = await res.json();
	const token = json.id_token ?? json.access_token;
	if (!token) throw new Error(`dex returned no token for ${email}`);
	return token;
}

/** Dex's subject is an opaque `CiQwOGE4…`, never "alice" — and it is simultaneously the FGA
 *  principal, the InboxActor id and what `enforce_author` stamps. All three must come from here. */
function subjectOf(token) {
	const json = Buffer.from(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
	const sub = JSON.parse(json).sub;
	if (!sub) throw new Error('token carries no sub');
	return sub;
}

async function fgaStoreId() {
	let page;
	do {
		const res = await fetch(`${FGA_API_URL}/stores?page_size=50${page ? `&continuation_token=${page}` : ''}`);
		if (!res.ok) throw new Error(`openfga /stores: HTTP ${res.status}`);
		const body = await res.json();
		const hit = (body.stores ?? []).filter((s) => s.name === FGA_STORE_NAME);
		if (hit.length) return hit.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at))).pop().id;
		page = body.continuation_token || null;
	} while (page);
	throw new Error(`no OpenFGA store named ${FGA_STORE_NAME}`);
}

/** Idempotent: a duplicate grant is success, so the drive is runnable twice in a row. */
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
		throw new Error(`grant ${key.user} ${key.relation} ${key.object}: HTTP ${res.status} ${text.slice(0, 160)}`);
	}
}

/** Unread rows for the token's holder, straight off the inbox door the bell reads. */
async function unread(token) {
	const res = await fetch(`${GATEWAY_URL}/api/notifications/inbox?filter=unread`, {
		headers: { authorization: `Bearer ${token}` },
	});
	if (!res.ok) throw new Error(`inbox: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
	const body = await res.json();
	return body.rows ?? body.items ?? body.notifications ?? [];
}

/** One terminal run. The author facet is deliberately NOT sent — `enforce_author` overwrites it, and
 *  sending one would invite the reader to believe it had an effect. `lance.originator` is the field
 *  under test; it is stamped by the producer and read by `originator_subject`. */
async function emitRun({ runId, originatorSub }) {
	const res = await fetch(`${LINEAGE_URL}/api/v1/lineage`, {
		method: 'POST',
		headers: { 'content-type': 'application/json', authorization: `Bearer ${aliceToken}` },
		body: JSON.stringify({
			eventType: 'FAIL',
			eventTime: new Date().toISOString(),
			producer: 'rask://verify_originator_lane',
			job: { namespace: 'rask-drive', name: `originator_${runId}` },
			run: { runId, facets: { lance: { operation: 'embed_features', originator: originatorSub } } },
			outputs: [{ namespace: OUTPUT.split('$')[0], name: OUTPUT }],
		}),
	});
	return { status: res.status, body: (await res.text().catch(() => '')).slice(0, 300) };
}

console.log(`\n▸ originator-lane drive against ${ORIGIN}\n`);

const aliceToken = await mintToken('alice@example.com');
const bobToken = await mintToken('bob@example.com');
const aliceSub = subjectOf(aliceToken);
const bobSub = subjectOf(bobToken);
console.log(`  alice = ${aliceSub}\n  bob   = ${bobSub}\n`);

// Both must be able to SEE the run's output or the delivery gate hides it — the plane re-derives
// visibility per recipient, which is exactly why an originator cannot be used to leak anything.
const storeId = await fgaStoreId();
await seedGrants(storeId, [
	{ user: `user:${aliceSub}`, relation: 'writer', object: `table:${OUTPUT}` },
	{ user: `user:${bobSub}`, relation: 'writer', object: `table:${OUTPUT}` },
]);

const before = { alice: (await unread(aliceToken)).length, bob: (await unread(bobToken)).length };
console.log(`  before: alice unread ${before.alice}, bob unread ${before.bob}`);

const runId = `originator-${Date.now()}`;
const emitted = await emitRun({ runId, originatorSub: bobSub });
check('the run was accepted by lineage', emitted.status < 300, `HTTP ${emitted.status} ${emitted.body}`);

await new Promise((r) => setTimeout(r, SETTLE_MS));

const after = { alice: (await unread(aliceToken)).length, bob: (await unread(bobToken)).length };
console.log(`  after:  alice unread ${after.alice}, bob unread ${after.bob}\n`);

check('alice was told as the AUTHOR (v1, the control)', after.alice === before.alice + 1, `${before.alice} -> ${after.alice}`);
check('bob was told as the ORIGINATOR (v5)', after.bob === before.bob + 1, `${before.bob} -> ${after.bob}`);

const bobRows = await unread(bobToken);
const row = bobRows.find((r) => String(r.notification_id ?? r.notificationId ?? '').startsWith(runId));
check('bob’s row exists for this run', Boolean(row), row ? '' : `saw ${bobRows.length} unread row(s)`);
if (row) {
	check('and its stored reason is `originator`, not `author`', row.reason === 'originator', `reason = ${row.reason}`);
}

console.log(`\n${failures === 0 ? '✅ all checks passed' : `❌ ${failures} check(s) failed`}\n`);
process.exit(failures === 0 ? 0 : 1);
