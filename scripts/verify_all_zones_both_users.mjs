/**
 * Conditions 9, 10 and 16 in one drive: BOTH users across ALL FOUR zones, against freshly built zone
 * images, with the new surface asserted inside its own panel and every suspected defect measured in the
 * DOM rather than judged by eye.
 *
 * Why each of those three is a separate condition, and why they belong in one script:
 *
 *   9  — a single-user drive proves the page renders, not that authorisation does anything. alice is an
 *        estate admin and bob is not; a zone that looks identical to both has no gate. So every zone is
 *        driven twice and the DIFFERENCE is the assertion.
 *   10 — a full-page screenshot at 1440×900 makes 12px text about 4px tall, which is how a clipped
 *        descender survived a review. Text-bearing regions are captured as ELEMENT crops at
 *        deviceScaleFactor 3, and anything suspected is measured (`clientHeight` vs `scrollHeight`)
 *        instead of squinted at.
 *   16 — the notification panel was previously asserted with `body.innerText()`, which passes if the
 *        string appears ANYWHERE on the page — including in the run table behind the popover. Every
 *        assertion here is scoped to `getByRole('dialog', {name: 'Notifications'})`.
 *
 * Run:  node scripts/verify_all_zones_both_users.mjs
 */
import { chromium } from '@playwright/test';

const ORIGIN = 'http://localhost:8090';
const SHOT = '/home/blackwell/Desktop/lance-ns/docs/audits/shots';
const ZONES = [
	{ name: 'home', path: '/' },
	{ name: 'lakehouse', path: '/lakehouse/' },
	{ name: 'media', path: '/media/' },
	{ name: 'annotator', path: '/annotator/' },
];

let failures = 0;
const check = (label, ok, detail = '') => {
	console.log(`   ${ok ? '✓' : '✗'} ${label}${detail ? ` — ${detail}` : ''}`);
	if (!ok) failures += 1;
};

const browser = await chromium.launch({
	args: ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'],
});

async function login(context, user, startPath) {
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent(startPath)}`, {
		waitUntil: 'domcontentloaded',
	});
	await page.waitForURL(/\/dex\/auth/, { timeout: 20000 });
	await page.fill('input[name="login"], input#login, input[type="text"], input[type="email"]', user);
	await page.fill('input[name="password"], input#password, input[type="password"]', 'password');
	await page.click('button[type="submit"], input[type="submit"], #submit-login');
	await page.waitForURL((u) => u.origin === ORIGIN && !u.pathname.startsWith('/auth'), {
		timeout: 25000,
	});
	return page;
}

/** WHO is signed in, from the account menu — initials alone are ambiguous, the email is not. */
async function identity(page) {
	const trigger = page.getByRole('button', { name: 'Account' });
	if ((await trigger.count()) === 0) return '';
	await trigger.first().click({ timeout: 10000 });
	const menu = page.locator('[role="menu"], [data-slot*="dropdown-menu-content"]').first();
	const text = ((await menu.textContent({ timeout: 5000 })) ?? '').replace(/\s+/g, ' ').trim();
	await page.keyboard.press('Escape');
	// The menu animates out. Screenshotting before it settles left a ghosted account card over the
	// toolbar in the first run's crops — legible, but evidence should not need explaining away.
	await page.waitForTimeout(400);
	return text;
}

/**
 * Condition 10's measurement: is this element's text actually visible, or clipped by its own line box?
 * Returns the numbers so the answer is a comparison rather than an impression.
 */
const measure = (locator) =>
	locator.evaluate((el) => {
		const cs = getComputedStyle(el);
		return {
			text: (el.textContent ?? '').trim().slice(0, 40),
			fontSize: cs.fontSize,
			lineHeight: cs.lineHeight,
			clientH: el.clientHeight,
			scrollH: el.scrollHeight,
			clientW: el.clientWidth,
			scrollW: el.scrollWidth,
			// Is the overflow ANNOUNCED? A `line-clamp` or an ellipsis draws a "…" and usually carries a
			// `title` with the whole string; a line box two pixels short of its glyphs draws nothing and
			// eats a descender. Only the second is a defect, and the first run of this script called both
			// "clipped" — a measurement conflating intent with accident is not a measurement.
			clamped: cs.webkitLineClamp !== 'none' && cs.webkitLineClamp !== '',
			ellipsis: cs.textOverflow === 'ellipsis',
			title: el.getAttribute('title') ?? '',
		};
	});

// ─── 9: both users, all four zones ────────────────────────────────────────────────────────────────
const seen = {};
for (const user of ['alice@example.com', 'bob@example.com']) {
	const who = user.split('@')[0];
	const ctx = await browser.newContext({
		viewport: { width: 1440, height: 900 },
		deviceScaleFactor: 3, // condition 10: crops must be legible, not 4px tall
	});
	const page = await login(ctx, user, '/lakehouse/');
	seen[who] = {};

	for (const zone of ZONES) {
		const res = await page.goto(`${ORIGIN}${zone.path}`, { waitUntil: 'domcontentloaded' });
		await page.waitForTimeout(1500);
		const status = res?.status() ?? 0;
		const id = await identity(page);
		seen[who][zone.name] = { status, id };
		check(
			`${who} reaches /${zone.name} and the shell knows who they are`,
			status < 400 && id.includes(user),
			`${status}, account menu: ${id.slice(0, 60) || '(none)'}`,
		);

		// The bell, in EVERY zone. It shipped in one of four — the component was shared, the transport was
		// not — and the zone where someone waits on a batch is the annotator, not the run board.
		const bells = await page.getByRole('button', { name: /notification/i }).count();
		check(`${who} sees the run bell in /${zone.name}`, bells === 1, `${bells} bell(s)`);

		await page.screenshot({ path: `${SHOT}/zones-${who}-${zone.name}.png`, fullPage: false });
	}

	// The authz difference — a zone that renders identically for both has no gate.
	const admin = await page.goto(`${ORIGIN}/lakehouse/admin/audit`, { waitUntil: 'domcontentloaded' });
	await page.waitForTimeout(1500);
	// The WHOLE region, not a 200-char slice: the first run read 200 characters of sidebar chrome,
	// concluded bob was "shown an empty table", and the screenshot said "Admin is estate-admin only".
	const adminBody = ((await page.locator('main').first().innerText()) ?? '').replace(/\s+/g, ' ');
	seen[who].admin = { status: admin?.status() ?? 0, body: adminBody };
	console.log(`   ${who} → /lakehouse/admin/audit: ${seen[who].admin.status} "${seen[who].admin.body.slice(0, 80)}"`);
	await page.screenshot({ path: `${SHOT}/zones-${who}-admin-audit.png`, fullPage: false });

	if (who === 'alice') {
		// ─── 16: the new surface, asserted INSIDE its own panel ───────────────────────────────────
		await page.goto(`${ORIGIN}/lakehouse/`, { waitUntil: 'domcontentloaded' });
		await page.waitForTimeout(2500);

		const feed = await page.evaluate(async () => {
			const r = await fetch('/lakehouse/api/runs');
			return { status: r.status, runs: r.ok ? ((await r.json()).runs ?? []) : [] };
		});
		const failed = feed.runs.filter((r) => r.state === 'FAIL' || r.state === 'ABORT');
		console.log(`   /lakehouse/api/runs -> ${feed.status}, ${feed.runs.length} runs, ${failed.length} failed`);

		const bell = page.getByRole('button', { name: /notification/i }).first();
		check('the navbar carries the bell', (await bell.count()) > 0);
		await bell.click();
		await page.waitForTimeout(1000);

		// THE scoping that condition 16 is about. Nothing below reads the page body.
		const panel = page.getByRole('dialog', { name: 'Notifications' });
		check('the bell opens a panel with its own role and name', (await panel.count()) === 1);

		const panelText = ((await panel.innerText()) ?? '').replace(/\s+/g, ' ');
		check('the panel — not the page — says Notifications', panelText.includes('Notifications'));

		if (failed.length > 0) {
			const first = failed[0];
			const err = (first.error_message ?? '').slice(0, 24);
			check(
				'a FAILED run is inside the panel, with its error, not just a red badge',
				panelText.includes('Failed') && (err === '' || panelText.includes(err)),
				err || '(no error_message on the first failure)',
			);
			// Failures first: the first status word in the panel must be Failed, since a failure sitting
			// past the window is a failure nobody can see.
			const firstState = (panelText.match(/Failed|Completed|Running|Started/) ?? [''])[0];
			check('failures sort above completions', firstState === 'Failed', `first state word: ${firstState}`);
		}

		// Condition 10: measure every text row in the panel rather than eyeballing the crop.
		const rows = panel.locator('p, span').filter({ hasText: /\S/ });
		const n = Math.min(await rows.count(), 12);
		let silent = 0;
		let unreachable = 0;
		for (let i = 0; i < n; i += 1) {
			const m = await measure(rows.nth(i));
			const overflows = m.clientH < m.scrollH || m.clientW < m.scrollW;
			if (!overflows) continue;
			if (!m.clamped && !m.ellipsis) {
				silent += 1;
				console.log(`     SILENTLY CUT: ${JSON.stringify(m)}`);
			} else if (m.title === '') {
				// Announced but unrecoverable: the reader can see there is more and has no way to get it.
				unreachable += 1;
				console.log(`     TRUNCATED WITH NO title=: ${JSON.stringify(m)}`);
			}
		}
		check(`no silently cut text in ${n} measured panel rows`, silent === 0, `${silent} cut without a marker`);
		check('every truncated row carries the full string in title=', unreachable === 0, `${unreachable} unrecoverable`);

		// The panel's own crop, at scale 3 — this is the "zoom on the text" half of condition 10.
		await panel.screenshot({ path: `${SHOT}/cond16-notification-panel.png` });
		console.log(`   crop: ${SHOT}/cond16-notification-panel.png (deviceScaleFactor 3)`);

		// Same surface, a DIFFERENT zone, with its own transport — the point of sharing the feed. The
		// annotator is the one that matters: it is where a person waits on the batch they are annotating
		// for, and it is where the bell was missing.
		for (const other of ['media', 'annotator']) {
			await page.goto(`${ORIGIN}/${other}/`, { waitUntil: 'domcontentloaded' });
			await page.waitForTimeout(3000);
			const otherBell = page.getByRole('button', { name: /notification/i }).first();
			if ((await otherBell.count()) === 0) {
				check(`the ${other} zone's bell opens a fed panel`, false, 'no bell');
				continue;
			}
			await otherBell.click();
			await page.waitForTimeout(1500);
			const otherPanel = page.getByRole('dialog', { name: 'Notifications' });
			const text = ((await otherPanel.innerText().catch(() => '')) ?? '').replace(/\s+/g, ' ');
			check(
				`the ${other} zone's bell opens a panel fed by ITS OWN transport`,
				(await otherPanel.count()) === 1 && /Failed|Completed|Running|Started/.test(text),
				text.slice(0, 70) || '(empty)',
			);
			await otherPanel
				.screenshot({ path: `${SHOT}/cond16-notification-panel-${other}.png` })
				.catch(() => {});
			await page.keyboard.press('Escape');
		}
	}

	await ctx.close();
}

// ─── the difference itself ────────────────────────────────────────────────────────────────────────
check(
	'alice and bob are told apart on the admin surface',
	seen.alice.admin.body !== seen.bob.admin.body,
	`alice "${seen.alice.admin.body.slice(0, 40)}" vs bob "${seen.bob.admin.body.slice(0, 40)}"`,
);
check(
	'bob is refused, and told WHY rather than shown an empty table',
	seen.bob.admin.status === 403 && /estate-admin|privilege|can_observe_events/i.test(seen.bob.admin.body),
	`${seen.bob.admin.status}: ${(seen.bob.admin.body.match(/[^.]*estate-admin[^.]*\./) ?? ['(no reason given)'])[0].trim()}`,
);
check(
	'and alice, who holds the privilege, gets the surface itself',
	seen.alice.admin.status === 200 && /Tenants|Audit|Streams|DLQ/.test(seen.alice.admin.body),
	`${seen.alice.admin.status}`,
);

await browser.close();
console.log(failures === 0 ? '\n✓ conditions 9, 10, 16 PROVEN' : `\n✗ ${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
