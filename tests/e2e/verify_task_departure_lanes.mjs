/** The two NEW departure lanes, driven against the deployed estate.
 *
 * `task_changes_requested` and `task_dropped` are proven by unit and integration tests, and the
 * deployed code was inspected in-pod. Neither of those is the claim that matters. The claim that
 * matters is that a PERSON's bell goes up — and this estate has already produced the failure where
 * every unit test was green, the ingress acked SUCCESS, and the row reached nobody. So this drives
 * the real annotator, the real Dapr control bus and the real inbox actor.
 *
 * WHY BOTH EDGES IN ONE RUN. They fail differently and a shared harness makes the difference legible:
 *   * `request_changes` reads its audience from `task.submitted_by`, which the actor writes once on
 *     submit and no edge ever clears;
 *   * `drop_task` reads `assignee` (falling back to `submitted_by`) from a snapshot taken BEFORE the
 *     drop, because afterwards the index entry naming the task is gone.
 * A regression in either would look identical from outside — a bell that does not move.
 *
 * Run:
 *   kubectl port-forward svc/rask-annotator 18113:8103
 *   kubectl port-forward svc/rask-gateway   18888:8888
 *   kubectl port-forward svc/rask-dex       15556:5556
 *   ANNOTATOR_URL=http://localhost:18113 GATEWAY_URL=http://localhost:18888 \
 *     TOKEN_URL=http://localhost:15556/dex/token node tests/e2e/verify_task_departure_lanes.mjs
 */

const ANNOTATOR_URL = process.env.ANNOTATOR_URL ?? 'http://localhost:18113';
const GATEWAY_URL = process.env.GATEWAY_URL ?? 'http://localhost:18888';
const TOKEN_URL = process.env.TOKEN_URL ?? 'http://localhost:15556/dex/token';
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

/** Dex's sub is opaque and is simultaneously the FGA principal AND the InboxActor id. */
function subjectOf(token) {
	const body = Buffer.from(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
	const sub = JSON.parse(body).sub;
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
		/* the status and text carry the story */
	}
	return { status: res.status, text: text.slice(0, 300), json };
}

/** The BADGE number — `unread`, never the length of one page of rows. */
async function unread(token) {
	const r = await api(`${GATEWAY_URL}/api/notifications/inbox?filter=unread`, token);
	if (r.status !== 200) throw new Error(`inbox: HTTP ${r.status} ${r.text}`);
	return Number(r.json?.unread ?? 0);
}

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

const settle = () => new Promise((r) => setTimeout(r, SETTLE_MS));

console.log(`\n▸ departure-lane drive — annotator ${ANNOTATOR_URL}, bell ${GATEWAY_URL}\n`);

const alice = await mintToken('alice@example.com');
const bob = await mintToken('bob@example.com');
const bobSub = subjectOf(bob);
console.log(`  bob = ${bobSub}\n`);

const stamp = Date.now();

async function seedTask(slug, taskId) {
	const created = await api(`${ANNOTATOR_URL}/projects`, alice, 'POST', {
		tenant: process.env.DRIVE_TENANT ?? 'acme',
		slug,
		title: `departure drive ${slug}`,
		review_required: true,
	});
	if (created.status >= 300) throw new Error(`project create: HTTP ${created.status} ${created.text}`);
	const projectId = created.json?.project_id ?? created.json?.id ?? slug;
	const sent = await api(`${ANNOTATOR_URL}/projects/${encodeURIComponent(projectId)}/items`, alice, 'POST', {
		items: [
			{
				task_id: taskId,
				source: { kind: 'corpus', keys: [`drive/${taskId}.png`] },
				media: { kind: 'image', image_url: `s3://drive/${taskId}.png`, width: 100, height: 100 },
			},
		],
	});
	if (sent.status >= 300) throw new Error(`send items: HTTP ${sent.status} ${sent.text}`);
	return { projectId, taskId: sent.json?.tasks?.[0]?.task_id ?? taskId };
}

// ── DROP: a manager discards an item somebody is holding ──────────────────────────────────────────
const dropCase = await seedTask(`drop-${stamp}`, `t-drop-${stamp}`);
await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(dropCase.taskId)}/events`, alice, 'POST', { event: 'assign', assignee: bobSub });
await settle();

const beforeDrop = await unread(bob);
const dropped = await api(
	`${ANNOTATOR_URL}/projects/${encodeURIComponent(dropCase.projectId)}/tasks/${encodeURIComponent(dropCase.taskId)}`,
	alice,
	'DELETE',
);
check('alice dropped the task bob was holding', dropped.status < 300, `HTTP ${dropped.status} ${dropped.text}`);
await settle();
const afterDrop = await unread(bob);
check('bob was told his held task was dropped', afterDrop === beforeDrop + 1, `${beforeDrop} -> ${afterDrop}`);

const dropRow = (await unreadRows(bob)).find((r) => String(r.object_id ?? '').includes(dropCase.taskId));
check('the row names the dropped task', Boolean(dropRow), dropRow ? `object_id = ${dropRow.object_id}` : 'no row names it');
if (dropRow) check('and its stored reason is `task_dropped`', dropRow.reason === 'task_dropped', `reason = ${dropRow.reason}`);

// ── REQUEST_CHANGES: a reviewer sends submitted work back ─────────────────────────────────────────
const rcCase = await seedTask(`rc-${stamp}`, `t-rc-${stamp}`);
await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(rcCase.taskId)}/events`, alice, 'POST', { event: 'assign', assignee: bobSub });
// BOB submits, so `submitted_by` is bob — the field the review side targets, and the reason alice
// (the reviewer) must not be the one told.
const submitted = await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(rcCase.taskId)}/events`, bob, 'POST', { event: 'submit' });
check('bob submitted the task for review', submitted.status < 300, `HTTP ${submitted.status} ${submitted.text}`);
await settle();

const beforeRc = await unread(bob);
const changes = await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(rcCase.taskId)}/events`, alice, 'POST', { event: 'request_changes' });
check('alice requested changes on bob’s submission', changes.status < 300, `HTTP ${changes.status} ${changes.text}`);
await settle();
const afterRc = await unread(bob);
check('bob was told his work came back', afterRc === beforeRc + 1, `${beforeRc} -> ${afterRc}`);

const rcRow = (await unreadRows(bob)).find((r) => String(r.object_id ?? '').includes(rcCase.taskId));
check('the row names the reopened task', Boolean(rcRow), rcRow ? `object_id = ${rcRow.object_id}` : 'no row names it');
if (rcRow) check('and its stored reason is `task_changes_requested`', rcRow.reason === 'task_changes_requested', `reason = ${rcRow.reason}`);

console.log(`\n${failures === 0 ? '✅ all checks passed' : `❌ ${failures} check(s) failed`}\n`);
process.exit(failures === 0 ? 0 : 1);
