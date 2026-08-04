import { test, expect, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// The warehouse DESTROY door (`DELETE /v1/warehouses/{id}`) as the user meets it. What is under test is
// not "does the button call the API" — it is that each of the three opt-ins costs something DELIBERATE:
//
//  * the plain delete goes first and its 409 is shown VERBATIM, because the catalog writes that sentence
//    to name the fix;
//  * `cascade` is offered only AFTER that refusal, and lists exactly the namespaces the refusal named
//    (there is no bindings read API — that sentence is the only authoritative source);
//  * `purge_bucket` — the only irreversible step in the estate's UI — stays inert until the bucket name
//    is typed, and DISARMS again the moment the typed name stops matching;
//  * `force` appears only when the refusal says the warehouse is protected.
//
// And that the success toast is built from the RESPONSE, never from the request: the catalog reports each
// store separately so a partial outcome is diagnosable, and a message that restated what was ASKED for
// would claim a purge the catalog refused to perform.
//
// The reads and the delete both run SERVER-SIDE (`warehouses.remote.ts` runs on the zone's Bun server),
// so `page.route` cannot reach them — the mock catalog's per-bearer seeded surface stands in, keyed by
// "METHOD /path?query". Seeding each query-string variant separately is what pins the exact request:
// an unseeded variant falls through to the mock's 404, so a delete that sent the wrong flags fails loudly.

const WAREHOUSE = {
	id: 'acme-wh',
	project: 'acme',
	bucket: 'acme-bucket',
	root_uri: 's3://acme-bucket',
	status: 'active',
	created_at: '2026-07-01T00:00:00Z',
};

/** The catalog's emptiness refusal, verbatim (endpoints/warehouses.py) — it NAMES what blocks the delete. */
const BOUND_DETAIL =
	"warehouse 'acme-wh' still holds 2 namespace(s): bronze, silver. " +
	'Drop them first, or pass ?cascade=true to drop exactly those with the warehouse.';

/** The protection refusal, verbatim (fga_deps.require_not_protected) — same 409, different lock. */
const PROTECTED_DETAIL =
	"warehouse 'acme-wh' is protected against deletion. Pass force=true to override " +
	'(protection only — the same authorization is still required), or clear the flag first.';

let token: string;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body, or {status, body}). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

/** Every mutating request this test's zone server made, newest last. */
const calls = async (page: Page): Promise<{ method: string; path: string }[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: { method: string; path: string }[] }).calls;
};

const deletePaths = async (page: Page): Promise<string[]> =>
	(await calls(page)).filter((c) => c.method === 'DELETE').map((c) => c.path);

// The RETRY index is part of the bearer, not just the test id. `testInfo.testId` is stable across
// attempts, so a retried test inherits the previous attempt's `__mock/calls` ledger — and every
// assertion in this file is an EXACT list of the deletes that were issued. Under CI's `retries: 1` a
// first-attempt flake would therefore fail its retry too, with a doubled diff that names the wrong
// defect. A fresh bearer per attempt gives each one a clean ledger; `identityOf` prefix-matches
// `TOKEN.admin`, so the identity is unchanged.
test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}:${testInfo.retry}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, { 'GET /v1/warehouses': [WAREHOUSE] });
});

/** Open the row's delete dialog. */
async function openDialog(page: Page) {
	await page.goto('/lakehouse/catalog/warehouses');
	// `exact` matters: the DataTable's ROW is itself `role="button"` (it opens the record drawer) and its
	// accessible name is the concatenation of its cells — which now contains this button's label too.
	await page.getByRole('button', { name: 'Delete warehouse acme-wh', exact: true }).click();
	const dialog = page.getByRole('dialog');
	await expect(dialog.getByText('Delete warehouse acme-wh')).toBeVisible();
	return dialog;
}

test('the plain delete goes first, its 409 is verbatim, and only then is cascade offered — naming what it would drop', async ({
	page,
}) => {
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh': { status: 409, body: { detail: BOUND_DETAIL } },
	});
	const dialog = await openDialog(page);

	// Nothing is pre-selected and nothing is pre-offered: the recoverable delete is the only thing on
	// the table until the catalog says otherwise.
	await expect(dialog.getByRole('checkbox', { name: /cascade/i })).toHaveCount(0);
	await expect(dialog.getByRole('checkbox', { name: /force/i })).toHaveCount(0);

	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();

	// VERBATIM — the whole sentence, not a paraphrase that loses the fix it names.
	await expect(dialog.getByTestId('delete-problem')).toHaveText(BOUND_DETAIL);
	// …and the cascade opt-in now exists, listing exactly the namespaces that sentence named.
	const cascade = dialog.getByRole('checkbox', { name: /cascade/i });
	await expect(cascade).toBeVisible();
	await expect(cascade).toHaveAttribute('aria-checked', 'false');
	await expect(dialog.getByTestId('cascade-namespaces')).toHaveText(/bronze/);
	await expect(dialog.getByTestId('cascade-namespaces')).toHaveText(/silver/);

	// The request carried NO flags — the plain delete really was tried first.
	expect(await deletePaths(page)).toEqual(['/v1/warehouses/acme-wh']);
});

test('the cascade delete sends exactly ?cascade=true and the toast reports the RESPONSE', async ({
	page,
}) => {
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh': { status: 409, body: { detail: BOUND_DETAIL } },
		'DELETE /v1/warehouses/acme-wh?cascade=true': {
			id: 'acme-wh',
			bucket: 'acme-bucket',
			namespaces_dropped: ['bronze', 'silver'],
			tuples_revoked: 7,
			bucket_purged: false,
			objects_purged: 0,
		},
	});
	const dialog = await openDialog(page);
	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();
	await expect(dialog.getByTestId('delete-problem')).toHaveText(BOUND_DETAIL);

	await dialog.getByRole('checkbox', { name: /cascade/i }).click();
	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();

	// Every number here comes from the response body: the namespaces it says it dropped, the tuple count
	// OpenFGA really returned, and — the one a request-shaped message would get wrong — that the bucket
	// was KEPT, which is what `bucket_purged: false` states.
	await expect(
		page.getByText(
			'Deleted warehouse acme-wh: dropped 2 namespaces (bronze, silver); revoked 7 tuples; bucket acme-bucket kept.',
		),
	).toBeVisible();

	expect(await deletePaths(page)).toEqual([
		'/v1/warehouses/acme-wh',
		'/v1/warehouses/acme-wh?cascade=true',
	]);
});

test('the purge stays inert until the bucket name is typed, and disarms when it stops matching', async ({
	page,
}) => {
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh?purge_bucket=true': {
			id: 'acme-wh',
			bucket: 'acme-bucket',
			namespaces_dropped: [],
			tuples_revoked: 3,
			bucket_purged: true,
			objects_purged: 413,
		},
	});
	const dialog = await openDialog(page);

	const purge = dialog.getByRole('checkbox', { name: /purge/i });
	const confirm = dialog.getByLabel('Type the bucket name to confirm the purge');
	const submit = dialog.getByRole('button', { name: /^Delete/ });

	// Offered, but unusable: the checkbox cannot be checked at all until the name is typed.
	await expect(purge).toBeDisabled();
	await confirm.fill('acme');
	await expect(purge).toBeDisabled();
	await confirm.fill('acme-bucket');
	await expect(purge).toBeEnabled();

	await purge.click();
	// The submit button states the destructive outcome — the label is the last warning.
	await expect(submit).toHaveText('Delete and purge bucket');

	// Editing the confirmation DISARMS it again: the consent was to destroy this bucket, not to have
	// once typed its name.
	await confirm.fill('acme-bucke');
	await expect(submit).toHaveText('Delete warehouse');
	await confirm.fill('acme-bucket');
	await expect(submit).toHaveText('Delete and purge bucket');

	await submit.click();
	await expect(
		page.getByText(
			'Deleted warehouse acme-wh: no namespaces dropped; revoked 3 tuples; purged bucket acme-bucket — 413 objects destroyed.',
		),
	).toBeVisible();

	expect(await deletePaths(page)).toEqual(['/v1/warehouses/acme-wh?purge_bucket=true']);
});

test('force is offered only after a protection refusal, and says what it does NOT override', async ({
	page,
}) => {
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh': { status: 409, body: { detail: PROTECTED_DETAIL } },
		'DELETE /v1/warehouses/acme-wh?force=true': {
			id: 'acme-wh',
			bucket: 'acme-bucket',
			namespaces_dropped: [],
			tuples_revoked: 1,
			bucket_purged: false,
			objects_purged: 0,
		},
	});
	const dialog = await openDialog(page);
	await expect(dialog.getByRole('checkbox', { name: /force/i })).toHaveCount(0);

	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();
	await expect(dialog.getByTestId('delete-problem')).toHaveText(PROTECTED_DETAIL);

	const force = dialog.getByRole('checkbox', { name: /force/i });
	await expect(force).toBeVisible();
	// Two independent locks, and force turns exactly one — the dialog must not let anyone read it as
	// a way past the authorization gate.
	await expect(dialog).toContainText('It is not an authorization override');
	// A protection refusal names no namespaces, so no cascade is offered by it.
	await expect(dialog.getByRole('checkbox', { name: /cascade/i })).toHaveCount(0);

	await force.click();
	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();
	await expect(
		page.getByText(
			'Deleted warehouse acme-wh: no namespaces dropped; revoked 1 tuple; bucket acme-bucket kept.',
		),
	).toBeVisible();

	expect(await deletePaths(page)).toEqual([
		'/v1/warehouses/acme-wh',
		'/v1/warehouses/acme-wh?force=true',
	]);
});

test('a DISARMED purge sends no purge_bucket, and the toast still reports the bucket as kept', async ({
	page,
}) => {
	// The submit label is a rendering of `purgeArmed`; this asserts the WIRE. Only the flagless variant is
	// seeded, so a request that carried `purge_bucket=true` would fall through to the mock's own key and
	// be recorded under a path this expectation rejects.
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh': {
			id: 'acme-wh',
			bucket: 'acme-bucket',
			namespaces_dropped: [],
			tuples_revoked: 2,
			bucket_purged: false,
			objects_purged: 0,
		},
	});
	const dialog = await openDialog(page);
	const confirm = dialog.getByLabel('Type the bucket name to confirm the purge');

	// Arm it fully — the checkbox is genuinely CHECKED at submit time — then break the confirmation.
	await confirm.fill('acme-bucket');
	await dialog.getByRole('checkbox', { name: /purge/i }).click();
	await expect(dialog.getByRole('checkbox', { name: /purge/i })).toHaveAttribute(
		'aria-checked',
		'true',
	);
	await confirm.fill('acme-bucke');

	await dialog.getByRole('button', { name: /^Delete/ }).click();

	// The message says the bucket SURVIVED — which is what the catalog answered, while the box the user
	// ticked says the opposite. A toast built from the request would claim a purge that never ran.
	// (Asserted BEFORE the call log: the log is only written when the request lands, so reading it
	// straight after the click races the round trip.)
	await expect(
		page.getByText(
			'Deleted warehouse acme-wh: no namespaces dropped; revoked 2 tuples; bucket acme-bucket kept.',
		),
	).toBeVisible();
	expect(await deletePaths(page)).toEqual(['/v1/warehouses/acme-wh']);
});

test('a confirmed delete re-reads the registry: the row is gone without a reload', async ({
	page,
}) => {
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh': {
			id: 'acme-wh',
			bucket: 'acme-bucket',
			namespaces_dropped: [],
			tuples_revoked: 2,
			bucket_purged: false,
			objects_purged: 0,
		},
	});
	const dialog = await openDialog(page);
	// From here the registry answers WITHOUT the warehouse. Nothing navigates, nothing reloads and the
	// control cursor never moves in this test — so the single-flight refresh inside `deleteWarehouse` is
	// the ONLY thing that can make the page stop showing a warehouse that no longer exists.
	await seed(page, { 'GET /v1/warehouses': [] });

	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();
	await expect(page.getByText('Deleted warehouse acme-wh:')).toBeVisible();
	await expect(page.getByRole('dialog')).toHaveCount(0);
	await expect(
		page.getByRole('button', { name: 'Delete warehouse acme-wh', exact: true }),
	).toHaveCount(0);
});

test('no opt-in survives a close: the next open starts from the plain delete again', async ({
	page,
}) => {
	await seed(page, {
		'DELETE /v1/warehouses/acme-wh': { status: 409, body: { detail: BOUND_DETAIL } },
	});
	const dialog = await openDialog(page);
	await dialog.getByRole('button', { name: 'Delete warehouse' }).click();
	await dialog.getByRole('checkbox', { name: /cascade/i }).click();
	await dialog.getByLabel('Type the bucket name to confirm the purge').fill('acme-bucket');
	await dialog.getByRole('checkbox', { name: /purge/i }).click();
	await expect(dialog.getByRole('button', { name: /^Delete/ })).toHaveText(
		'Delete and purge bucket',
	);

	await dialog.getByRole('button', { name: 'Cancel' }).click();
	await expect(page.getByRole('dialog')).toHaveCount(0);

	const reopened = await openDialog(page);
	// Consent given once is not consent for the next attempt: no refusal on screen, no cascade offered,
	// the purge confirmation empty and the purge unchecked.
	await expect(reopened.getByTestId('delete-problem')).toHaveCount(0);
	await expect(reopened.getByRole('checkbox', { name: /cascade/i })).toHaveCount(0);
	await expect(reopened.getByLabel('Type the bucket name to confirm the purge')).toHaveValue('');
	await expect(reopened.getByRole('checkbox', { name: /purge/i })).toBeDisabled();
	await expect(reopened.getByRole('button', { name: /^Delete/ })).toHaveText('Delete warehouse');
});
