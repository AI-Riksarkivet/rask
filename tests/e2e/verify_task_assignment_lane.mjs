/** The TASK-ASSIGNMENT lane, driven against the deployed estate.
 *
 * The claim: alice assigns bob a task, and BOB's bell goes up while ALICE's does not. Before this,
 * the annotator emitted nothing at all, so an assignee learned about their own work by going to look
 * for it.
 *
 * The lane is deliberately the CONTROL one, not lineage: an assignment is not a run (no terminal
 * event, no output dataset) and the actor is the MANAGER while the audience is the worker. It is the
 * same shape as a grant — a governance act that NAMES a person — and being named IS the targeting.
 * That is also why bob needs no grant on anything here: the control lane runs no visibility check,
 * because after a `grant_revoked` the subject can no longer see the object and a delivery-time check
 * would drop the one event they most need.
 *
 * REACHED DIRECTLY, not through the gateway. The annotator's task doors carry no gateway row (only
 * `/api/explorer/annotations` does), so this needs a port-forward:
 *
 *   kubectl port-forward svc/rask-annotator 18103:8103
 *   ORIGIN=http://localhost:8080 ANNOTATOR_URL=http://localhost:18103 node tests/e2e/verify_task_assignment_lane.mjs
 *
 * In-cluster prerequisites, and BOTH fail silently when missing:
 *   * ANNOTATOR_CONTROL_EMIT_ENABLED=true — otherwise `make_control_emitter` returns the no-op;
 *   * `annotator` in the control pubsub component's `scopes:` — otherwise the sidecar rejects each
 *     publish, and the emit is best-effort so nothing surfaces it.
 */

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8080';
const ANNOTATOR_URL = process.env.ANNOTATOR_URL ?? 'http://localhost:18103';
const GATEWAY_URL = process.env.GATEWAY_URL ?? ORIGIN;
const TOKEN_URL = process.env.TOKEN_URL ?? `${ORIGIN}/dex/token`;
const CLIENT_ID = process.env.OIDC_CLIENT_ID ?? 'lance-catalog';
const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? 'lance-catalog-secret';
const PASSWORD = process.env.DEX_PASSWORD ?? 'password';
const SETTLE_MS = Number(process.env.SETTLE_MS ?? 6000);

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

/** Dex's subject is opaque (`CiQwOGE4…`) and is simultaneously the FGA principal AND the InboxActor
 *  id. The assignee must be spelled this way or the row lands in an inbox nobody owns. */
function subjectOf(token) {
	const json = Buffer.from(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
	const sub = JSON.parse(json).sub;
	if (!sub) throw new Error('token carries no sub');
	return sub;
}

async function api(url, token, method = 'GET', body) {
	const res = await fetch(url, {
		method,
		headers: { authorization: `Bearer ${token}`, ...(body ? { 'content-type': 'application/json' } : {}) },
		...(body ? { body: JSON.stringify(body) } : {}),
	});
	const text = await res.text().catch(() => '');
	let json = null;
	try {
		json = JSON.parse(text);
	} catch {
		/* non-JSON body — the status and text carry the story */
	}
	return { status: res.status, text: text.slice(0, 300), json };
}

/** The BADGE number, which is `unread` and NOT the length of `notifications`.
 *
 *  Those differ, and the difference cost this drive a false negative: the door returns one PAGE of
 *  rows (13 here) beside the true count, so counting the array reported "no change" while the count
 *  had gone 13 -> 14. The badge the person actually sees is `unread`; anything else is a page size. */
async function unreadCount(token) {
	const r = await api(`${GATEWAY_URL}/api/notifications/inbox?filter=unread`, token);
	if (r.status !== 200) throw new Error(`inbox: HTTP ${r.status} ${r.text}`);
	return Number(r.json?.unread ?? 0);
}

/** Every unread row, following `next_cursor` — a control row is not necessarily on the first page. */
async function unreadRows(token) {
	const out = [];
	let cursor = null;
	for (let i = 0; i < 10; i += 1) {
		const url = `${GATEWAY_URL}/api/notifications/inbox?filter=unread${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
		const r = await api(url, token);
		out.push(...(r.json?.notifications ?? []));
		cursor = r.json?.next_cursor ?? null;
		if (!cursor) break;
	}
	return out;
}

console.log(`\n▸ task-assignment drive — annotator ${ANNOTATOR_URL}, bell ${GATEWAY_URL}\n`);

const aliceToken = await mintToken('alice@example.com');
const bobToken = await mintToken('bob@example.com');
const aliceSub = subjectOf(aliceToken);
const bobSub = subjectOf(bobToken);
console.log(`  alice = ${aliceSub}\n  bob   = ${bobSub}\n`);

const stamp = Date.now();
const slug = `assign-drive-${stamp}`;

// Alice creates the project, which seeds HER ownership — she is the manager whose `can_manage` the
// assign edge is gated on. Driving the real door rather than writing tuples is the point: a seeded
// grant would prove nothing about the gate the assignment actually passes through.
const created = await api(`${ANNOTATOR_URL}/projects`, aliceToken, 'POST', {
	tenant: process.env.DRIVE_TENANT ?? 'acme',
	slug,
	title: `assignment drive ${stamp}`,
	review_required: false,
});
check('alice created the annotation project', created.status < 300, `HTTP ${created.status} ${created.text}`);
const projectId = created.json?.project_id ?? created.json?.id ?? slug;

const sent = await api(`${ANNOTATOR_URL}/projects/${encodeURIComponent(projectId)}/items`, aliceToken, 'POST', {
	items: [
		{
			task_id: `t-${stamp}`,
			source: { kind: 'corpus', keys: [`drive/${stamp}.png`] },
			media: { kind: 'image', image_url: `s3://drive/${stamp}.png`, width: 100, height: 100 },
		},
	],
});
check('an item was sent in as a task', sent.status < 300, `HTTP ${sent.status} ${sent.text}`);

const taskId = sent.json?.tasks?.[0]?.task_id ?? sent.json?.task_ids?.[0] ?? sent.json?.tasks?.[0] ?? `${projectId}-item-${stamp}`;
console.log(`  project = ${projectId}\n  task    = ${taskId}\n`);

const before = { alice: await unreadCount(aliceToken), bob: await unreadCount(bobToken) };
console.log(`  before: alice unread ${before.alice}, bob unread ${before.bob}`);

// THE ACT UNDER TEST. `assignee` is bob; the verified caller is alice. The plane must target the
// former and not the latter — keying on the actor is what would tell managers about their own clicks.
const assigned = await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(taskId)}/events`, aliceToken, 'POST', {
	event: 'assign',
	assignee: bobSub,
});
check('alice assigned the task to bob', assigned.status < 300, `HTTP ${assigned.status} ${assigned.text}`);

await new Promise((r) => setTimeout(r, SETTLE_MS));

const after = { alice: await unreadCount(aliceToken), bob: await unreadCount(bobToken) };
console.log(`  after:  alice unread ${after.alice}, bob unread ${after.bob}\n`);

check('bob was told (0 -> 1 on this event)', after.bob === before.bob + 1, `${before.bob} -> ${after.bob}`);
check('alice was NOT told about her own click', after.alice === before.alice, `${before.alice} -> ${after.alice}`);

const row = (await unreadRows(bobToken)).find((r) => String(r.object_id ?? '').includes(taskId));
check('bob’s row names the task', Boolean(row), row ? `object_id = ${row.object_id}` : 'no row naming the task');
if (row) check('and its stored reason is `task_assigned`', row.reason === 'task_assigned', `reason = ${row.reason}`);

console.log(`\n${failures === 0 ? '✅ all checks passed' : `❌ ${failures} check(s) failed`}\n`);
process.exit(failures === 0 ? 0 : 1);
