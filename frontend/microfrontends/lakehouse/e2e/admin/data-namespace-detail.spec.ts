import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic /namespaces/<id> coverage (sweep group 3): the namespace detail page surfaces the
// catalog's namespace-scoped governance — the table roster (from the same /v1/table list the
// registry groups), the #50 maintenance-policy card (describe/set/delete) and the kind-generalized
// GrantsPanel (list/grant/revoke against the NAMESPACE surface). All of it rides remote functions
// now (the transport ruling, area 1), so the wire is seeded on the mock catalog and the writes are
// asserted through its per-bearer ledger — same assertions as the /capi-era spec, including the one
// that matters most: the writes land on the NAMESPACE paths, never the table ones.

type Body = Record<string, unknown>;

const TABLES = { tables: ['bronze$events', 'gold$catalog', 'gold$metrics'] };

const POLICY = {
	kind: 'namespace',
	id: 'gold',
	path: 'lance-catalog/gold',
	compact_enabled: true,
	retention_days: 14,
	retain_versions: 5,
};

const ACL = {
	object: 'namespace:gold',
	grants: [
		{ relation: 'can_read_data', users: ['user:alice'] },
		{ relation: 'can_delete', users: ['user:alice'] },
	],
};

/** The signed-in caller's OWN verdicts — what the page renders its actions from. Distinct from `ACL`
 *  above, which is who-holds-what and is owner-gated; this one describes only the caller. */
const MY_PERMISSIONS = {
	object: 'namespace:gold',
	subject: 'user:alice',
	permissions: { can_get_metadata: true, can_delete: true, can_update_properties: true },
};

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

const callTo = async (page: Page, path: string): Promise<Body | undefined> =>
	(await calls(page)).find((c) => c.path === path);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, {
		'GET /v1/table': TABLES,
		'POST /v1/namespace/gold/policy/describe': POLICY,
		// The write echoes the drafted bounds, exactly as the catalog does.
		'POST /v1/namespace/gold/policy/set': { ...POLICY, retention_days: 30 },
		// The ENVELOPE form is mandatory here, not stylistic: this payload carries its own `status`
		// field ("deleted"), which the bare form would read as the HTTP status and answer 500.
		'POST /v1/namespace/gold/policy/delete': {
			status: 200,
			body: { status: 'deleted', kind: 'namespace', id: 'gold' },
		},
		'POST /v1/namespace/gold/access/list': ACL,
		// The SELF-view. Seeded for every test in this file because the policy card renders FROM it:
		// unseeded it 404s, `perms` stays unanswered, and Edit/Remove/Set policy are correctly absent —
		// which would fail the three specs below as a missing button rather than as the authz story it
		// actually is. `can_delete` is the rung the catalog gates policy writes on, so this is the
		// admin these tests have always been signed in as, now stated on the wire instead of assumed.
		'POST /v1/namespace/gold/access/my-permissions': MY_PERMISSIONS,
		'POST /v1/namespace/gold/access/grant': {
			object: 'namespace:gold',
			user: 'user:bob',
			relation: 'reader',
			granted: true,
		},
	});
});

test('renders the header, table roster, and policy chips from the mocked list + policy', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await expect(page.getByRole('heading', { name: 'gold', exact: true })).toBeVisible();
	// The count comes from the registry list, grouped by the `<namespace>$` prefix. Scoped to the page's
	// own header: the AppShell topbar and the access-graph card both contribute <header> elements too.
	await expect(page.locator('.page > header')).toContainText('2 tables');
	await expect(page.locator('a.row', { hasText: 'gold$catalog' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/tables/gold%24catalog',
	);
	// bronze's table must NOT bleed into gold's roster.
	await expect(page.locator('a.row', { hasText: 'bronze$events' })).toHaveCount(0);
	// The #50 policy card renders the described policy as chips.
	await expect(page.locator('.chip', { hasText: 'retention 14d' })).toBeVisible();
	await expect(page.locator('.chip', { hasText: 'keep last 5' })).toBeVisible();
});

test('a grant posts the exact {user, relation} body to the NAMESPACE access route', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await expect(page.locator('table.acl')).toContainText('can_delete');
	await page.getByPlaceholder('user (e.g. alice), or role:… / team:…#member').last().fill('bob');
	// The manage form's rung picker is the @rask/ui Select (bits-ui) — open by aria-label, click option.
	await page.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: 'reader', exact: true }).click();
	await page.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect(page.locator('.verdict.allow')).toContainText('granted to');
	// Poll (don't race the ledger) — and pin the PATH so a regression that silently falls back to the
	// table surface can't pass.
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/gold/access/grant'))
		.toMatchObject({ method: 'POST', body: { user: 'bob', relation: 'reader' } });
	expect(await callTo(page, '/v1/table/gold/access/grant')).toBeUndefined();
});

test('editing the policy posts the drafted bounds to the namespace policy route', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('button', { name: 'Edit', exact: true }).click();
	// The write refreshes the describe read in the same flight, so the card re-renders from the
	// catalog's POST-write state, never from a local splice. Model that here: a real catalog would
	// describe the policy it just stored.
	await seed(page, {
		'POST /v1/namespace/gold/policy/describe': { ...POLICY, retention_days: 30 },
	});
	await page.getByLabel('retention days').fill('30');
	await page.getByRole('button', { name: 'Save policy' }).click();
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/gold/policy/set'))
		.toMatchObject({
			method: 'POST',
			body: { compact_enabled: true, retention_days: 30, retain_versions: 5 },
		});
	// The card re-renders from the server's post-write state — latest bounds, no stale chips. A longer
	// budget than the 5s default: this render now waits on the write AND the describe it refreshes,
	// two server round trips on a dev server the whole suite shares.
	await expect(page.locator('.chip', { hasText: 'retention 30d' })).toBeVisible({
		timeout: 15_000,
	});
});

test('removing the policy deletes it and the card falls back to the global-defaults state', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await expect(page.locator('.chip', { hasText: 'retention 14d' })).toBeVisible();
	// Same single-flight refresh: after the delete the catalog describes NO policy (404), which is the
	// honest "global defaults apply" state — not an error.
	await seed(page, {
		'POST /v1/namespace/gold/policy/describe': { status: 404, body: { detail: 'no policy' } },
	});
	await page.getByRole('button', { name: 'Remove', exact: true }).click();
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/gold/policy/delete'))
		.toMatchObject({ method: 'POST' });
	// Same two-round-trip budget as the set case above.
	await expect(page.getByText('No policy — the sweep applies the global defaults.')).toBeVisible({
		timeout: 15_000,
	});
});

test('a 403 access review renders the denial state, never the ACL', async ({ page }) => {
	// Re-seeding the same key wins — deny the review like the catalog's FGA gate (owner-tier
	// can_delete on the namespace) does for a non-owner.
	await seed(page, {
		'POST /v1/namespace/gold/access/list': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await expect(
		page.getByText('Owner access required to review who can reach this namespace.'),
	).toBeVisible();
	await expect(page.locator('table.acl')).toHaveCount(0);
});

test('a 403 policy write surfaces the owner-rung denial on the card', async ({ page }) => {
	await seed(page, {
		'POST /v1/namespace/gold/policy/set': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('button', { name: 'Edit', exact: true }).click();
	await page.getByRole('button', { name: 'Save policy' }).click();
	await expect(
		page.getByText('Denied: policy changes need the owner rung (can_delete).'),
	).toBeVisible();
});

test('the registry links each namespace name to its detail page', async ({ page }) => {
	await page.goto('/lakehouse/catalog/namespaces');
	await expect(page.locator('a.ns-name', { hasText: 'gold' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/namespaces/gold',
	);
	await page.locator('a.ns-name', { hasText: 'gold' }).click();
	// The zone is served under /lakehouse and the registry lives at /catalog/namespaces — the old copy
	// of this assertion pinned a `/data/…` path this route has not had since the zone merge, so it
	// failed on the pre-migration suite too.
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/namespaces\/gold$/);
	await expect(page.getByRole('heading', { name: 'gold', exact: true })).toBeVisible();
});

// UPWARD VISIBILITY made this page reachable by someone who cannot act on it: a grant on ONE table
// now lights `can_get_metadata` on every namespace above it, so a reader legitimately lands here.
// Before the self-view the page rendered Edit / Remove / Set policy to that person unconditionally
// and only reported the denial after the click — a button whose sole purpose was to fail.
test('a caller who may only SEE the namespace gets no policy actions, and is told which rung is missing', async ({
	page,
}) => {
	await seed(page, {
		// The single-table grantee: visible, and nothing more. `can_delete` false is the whole test.
		'POST /v1/namespace/gold/access/my-permissions': {
			object: 'namespace:gold',
			subject: 'user:ivan',
			permissions: { can_get_metadata: true, can_delete: false, can_update_properties: false },
		},
	});
	await page.goto('/lakehouse/catalog/namespaces/gold');

	// The page still RENDERS — visibility is the point of the child edge.
	await expect(page.getByRole('heading', { name: 'gold', exact: true })).toBeVisible();
	// …and it names the missing rung rather than going silent, which would read as a broken page.
	await expect(page.getByText('Changing this policy needs the owner rung')).toBeVisible({
		timeout: 15_000,
	});
	// The three actions are ABSENT, not disabled: a disabled control still advertises the operation.
	await expect(page.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Remove', exact: true })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Set policy', exact: true })).toHaveCount(0);
});

// The GRANT AXIS. Granting used to be a side-effect of `owner`, so the picker offered four data
// rungs and nothing else — there was no way to say "administer access but read nothing". These two
// are the axis, and they carry explanatory labels because picking the wrong one hands out the
// authority to hand out authority.
test('the grant picker offers the grant-axis rungs, labelled for what they do', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await page.getByLabel('Grant rung').click();

	// The four data rungs keep their bare names — `reader` means what it says.
	await expect(page.getByRole('option', { name: 'reader', exact: true })).toBeVisible();
	await expect(page.getByRole('option', { name: 'owner', exact: true })).toBeVisible();
	// The grant axis does not, so it is spelled out in the option itself.
	await expect(
		page.getByRole('option', { name: /manage_grants — may grant anything/ }),
	).toBeVisible();
	await expect(
		page.getByRole('option', { name: /pass_grants — may re-grant what they hold/ }),
	).toBeVisible();
});

// MANAGED ACCESS (C4). The flag is the REASON an owner's grant controls are absent, so the page has
// to say so — an unexplained absence is indistinguishable from a bug, and the person best placed to
// misread it is the owner who expected to be able to grant.
test('a namespace with centralized granting says so, to the owner it constrains', async ({
	page,
}) => {
	await seed(page, {
		'POST /v1/namespace/gold/managed-access/describe': {
			object: 'namespace:gold',
			managed_access: true,
		},
	});
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await expect(page.getByText(/Granting here is/)).toBeVisible({ timeout: 15_000 });
	await expect(page.getByText(/a grant-manager on the warehouse above does that/)).toBeVisible();
});

test('an UNMANAGED namespace stays silent — no policy, no notice', async ({ page }) => {
	await seed(page, {
		'POST /v1/namespace/gold/managed-access/describe': {
			object: 'namespace:gold',
			managed_access: false,
		},
	});
	await page.goto('/lakehouse/catalog/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await expect(page.getByRole('button', { name: 'Access review' })).toBeVisible({
		timeout: 15_000,
	});
	await expect(page.getByText(/Granting here is/)).toHaveCount(0);
});
