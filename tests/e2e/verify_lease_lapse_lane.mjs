/** The LEASE-LAPSE lane, driven against the deployed estate.
 *
 * The one departure edge no person causes. Bob claims a task with a deliberately short lease and then
 * does nothing; the annotator's own Dapr reminder fires, the task returns to the pool, and Bob's bell
 * must go up — for a transition triggered by a timer, in a process with no request in flight.
 *
 * WHY THIS ONE NEEDS A LIVE DRIVE MORE THAN THE OTHERS. Its emitter does not come from a FastAPI
 * dependency: an actor has no `Request`, so the reminder reads a PROCESS-level handle that the
 * lifespan sets (`set_process_control_emitter`). A unit test can monkeypatch that handle and prove the
 * actor emits; only a real pod proves the lifespan actually set it. Get that wrong and every unit test
 * still passes while the estate emits through the no-op — which is exactly the silent shape this whole
 * body of work exists to remove.
 *
 * `lease_seconds` is a documented per-claim override (`FireRequest`, `gt=0`), so the drive does not
 * wait 30 minutes for the project default.
 *
 * Run:
 *   kubectl port-forward svc/rask-annotator 18113:8103
 *   kubectl port-forward svc/rask-gateway   18888:8888
 *   kubectl port-forward svc/rask-dex       15556:5556
 *   ANNOTATOR_URL=http://localhost:18113 GATEWAY_URL=http://localhost:18888 \
 *     TOKEN_URL=http://localhost:15556/dex/token node tests/e2e/verify_lease_lapse_lane.mjs
 */

const ANNOTATOR_URL = process.env.ANNOTATOR_URL ?? 'http://localhost:18113';
const GATEWAY_URL = process.env.GATEWAY_URL ?? 'http://localhost:18888';
const TOKEN_URL = process.env.TOKEN_URL ?? 'http://localhost:15556/dex/token';
const CLIENT_ID = process.env.OIDC_CLIENT_ID ?? 'lance-catalog';
const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? 'lance-catalog-secret';
const PASSWORD = process.env.DEX_PASSWORD ?? 'password';
const LEASE_SECONDS = Number(process.env.LEASE_SECONDS ?? 5);
/** The reminder fires at the lease, then the emit crosses the control bus and the inbox actor. */
const SETTLE_MS = Number(process.env.SETTLE_MS ?? 20000);

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
	if (!res.ok) throw new Error(`dex refused ${email}: HTTP ${res.status}`);
	const json = await res.json();
	return json.id_token ?? json.access_token;
}

function subjectOf(token) {
	const body = Buffer.from(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
	return JSON.parse(body).sub;
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
		/* status + text carry the story */
	}
	return { status: res.status, text: text.slice(0, 240), json };
}

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

console.log(`\n▸ lease-lapse drive — annotator ${ANNOTATOR_URL}, bell ${GATEWAY_URL}\n`);

const alice = await mintToken('alice@example.com');
const bob = await mintToken('bob@example.com');
const bobSub = subjectOf(bob);

const stamp = Date.now();
const created = await api(`${ANNOTATOR_URL}/projects`, alice, 'POST', {
	tenant: process.env.DRIVE_TENANT ?? 'acme',
	slug: `lease-${stamp}`,
	title: `lease drive ${stamp}`,
	review_required: false,
});
check('alice created the project', created.status < 300, `HTTP ${created.status} ${created.text}`);
const projectId = created.json?.project_id ?? created.json?.id ?? `lease-${stamp}`;

const taskId = `t-lease-${stamp}`;
const sent = await api(`${ANNOTATOR_URL}/projects/${encodeURIComponent(projectId)}/items`, alice, 'POST', {
	items: [
		{
			task_id: taskId,
			source: { kind: 'corpus', keys: [`drive/${taskId}.png`] },
			media: { kind: 'image', image_url: `s3://drive/${taskId}.png`, width: 100, height: 100 },
		},
	],
});
check('an item was sent in', sent.status < 300, `HTTP ${sent.status} ${sent.text}`);

const before = await unread(bob);

// BOB CLAIMS IT HIMSELF. That is the only path that arms a lease — `assign` pins
// `lease_expires_at = None` and never expires, which is the design and not a gap.
const claimed = await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(taskId)}/events`, bob, 'POST', {
	event: 'claim',
	lease_seconds: LEASE_SECONDS,
});
check(`bob claimed it with a ${LEASE_SECONDS}s lease`, claimed.status < 300, `HTTP ${claimed.status} ${claimed.text}`);

console.log(`  waiting ${SETTLE_MS / 1000}s for the reminder, the bus and the inbox actor…`);
await new Promise((r) => setTimeout(r, SETTLE_MS));

const task = await api(`${ANNOTATOR_URL}/tasks/${encodeURIComponent(taskId)}`, alice);
check('the lease actually lapsed (task back in the pool)', task.json?.state === 'unassigned', `state = ${task.json?.state}`);

const after = await unread(bob);
check('bob was told his hold lapsed', after === before + 1, `${before} -> ${after}`);

const row = (await unreadRows(bob)).find((r) => String(r.object_id ?? '').includes(taskId));
check('the row names the task', Boolean(row), row ? `object_id = ${row.object_id}` : 'no row names it');
if (row) check('and its stored reason is `task_lease_expired`', row.reason === 'task_lease_expired', `reason = ${row.reason}`);

console.log(`\n${failures === 0 ? '✅ all checks passed' : `❌ ${failures} check(s) failed`}\n`);
process.exit(failures === 0 ? 0 : 1);
