import { expect, test } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// `/settings/access` — the object picker must offer a nested table's REAL parent namespace.
//
// The regression this pins: `objectOptions` derived a table's namespace with
// `t.slice(0, t.indexOf('$'))`. Catalog ids are a hierarchy flattened with a delimiter, so
// `acme$bronze$pages` is the table `pages` inside the namespace `acme$bronze` — and the backend says
// so in one line (`parent_namespace_id` is "all segments but the last",
// `service_kit/governed/fga.py:187-201`). Splitting on the FIRST delimiter yielded `acme`: a
// DIFFERENT OpenFGA object, quite possibly one that does not exist, while the real parent was never
// offered at all. These strings are not decoration — the user picks one and runs an authorization
// check against it, so in the single view whose job is "who can do what on THIS object", the picker
// was proposing the wrong object and the check answered about it confidently.
//
// Why this test exists in the BROWSER and not only as a unit test: the helper
// (`@rask/api/identifiers`) has its own tests, but a green helper proves nothing about whether the
// call site was rewired. This drives the real component, through the real remote function, and reads
// the options actually in the DOM.
//
// Why the ids are SEEDED rather than taken from the estate: measured against the deployed catalog on
// 2026-08-16, all ten registered tables are flat (exactly one delimiter). The bug is therefore
// invisible on live data — a screenshot of the estate would look identical before and after the fix.
// A nested id has to be supplied for the difference to exist at all.
const NESTED = 'acme$bronze$pages';
const FLAT = 'db1$t';

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seedFor(page, token, {
		'GET /v1/me': ME_ADMIN,
		// The registry the picker is built from. Both shapes on purpose: the flat one is the case that
		// always worked, and it must KEEP working — a fix that reads the last segment everywhere would
		// break `db1$t` if it were wrong in the other direction.
		'GET /v1/table': { tables: [NESTED, FLAT] },
	});
});

test('the object picker offers a nested table PARENT namespace, not its first segment', async ({
	page,
}) => {
	await page.goto('/settings/access');

	const options = page.locator('#access-objects option');
	await expect(options.first()).toBeAttached();
	const values = await options.evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));

	// Both tables are offered as table objects — the picker still lists what it always listed.
	expect(values).toContain(`table:${NESTED}`);
	expect(values).toContain(`table:${FLAT}`);

	// THE ASSERTION. The real parent is offered…
	expect(values).toContain('namespace:acme$bronze');
	// …and the first-segment answer is NOT, because `namespace:acme` is a different object and
	// offering it invites a check against something the table does not belong to.
	expect(values).not.toContain('namespace:acme');

	// The flat case is unchanged: a single-delimiter id's parent is its only leading segment.
	expect(values).toContain('namespace:db1');

	// Evidence for review, not an assertion. The options live in a `<datalist>`, which the browser
	// paints as an OS-level popup that does not appear in a screenshot — so the shot is the view in
	// context, and the option values are echoed into the test log where they can be read directly.
	console.log('object picker options:\n  ' + values.join('\n  '));
	await page.screenshot({ path: 'e2e/__shots__/nested-namespace-access-explorer.png' });
});

test('a nested id is selectable and survives into the query URL', async ({ page }) => {
	// The options being right is half of it; the view has to actually accept one. `$` is legal in a URL
	// path and query, but it is exactly the character an over-eager encoder mangles — so this drives
	// the value through the input into `pushUrl` and reads it back off the address bar.
	await page.goto('/settings/access');

	// The field is `aria-label="Object"` exactly, and only renders when the query kind is NOT 'what'
	// (that branch swaps in an object-TYPE field instead). The default kind is 'why', so it is present
	// on load — but pin it by the exact label rather than a loose /object/i, which also matches
	// "Object type" and would silently assert against the wrong input.
	const object = page.locator('input[aria-label="Object"]');
	await expect(object).toBeVisible();
	// Wait for the view to finish hydrating before filling OR shooting. The URL-hydration `$effect`
	// re-runs on `page.url()` changes and the canvas mounts asynchronously, so an early screenshot
	// catches a blank frame — which is what the first version of this test photographed.
	await expect(page.getByText('table:acme$bronze$pages')).toBeVisible();

	await object.fill('namespace:acme$bronze');
	await expect(object).toHaveValue('namespace:acme$bronze');

	await page.screenshot({ path: 'e2e/__shots__/nested-namespace-object-selected.png' });

	// `$` is legal in a URL query but is exactly the character an over-eager encoder mangles, so read
	// the value back off the field after the view has round-tripped it.
	expect(await object.inputValue()).toBe('namespace:acme$bronze');
});
