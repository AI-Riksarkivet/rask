import { chromium } from '@playwright/test';
const OUT =
	'/tmp/claude-1000/-home-blackwell-Desktop-rask/0d747d65-2a04-47cb-be0b-c417d0f6e643/scratchpad';
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1600, height: 950 } });
const p = await ctx.newPage();
await p.goto('http://localhost/', { waitUntil: 'domcontentloaded' });
if ((await p.locator('input[type="password"]').count()) > 0) {
	await p.fill('input[type="text"], input[type="email"]', 'alice@example.com');
	await p.fill('input[type="password"]', 'password');
	await p.click('button[type="submit"], input[type="submit"], #submit-login');
	await p.waitForLoadState('load');
}
await p.goto('http://localhost/workbench/', { waitUntil: 'load' });
await p.locator('.dv-dockview').waitFor({ state: 'visible', timeout: 20000 });
await p.waitForTimeout(5000);
// walk EVERY tab, activate it, report its visible first lines
const tabs = await p.locator('.dv-tab').allInnerTexts();
console.log('tabs present:', JSON.stringify(tabs));
for (const t of [...new Set(tabs)].filter(Boolean)) {
	await p
		.locator('.dv-tab', { hasText: t })
		.first()
		.click()
		.catch(() => {});
	await p.waitForTimeout(1500);
	const active = p.locator('.dv-groupview .dv-active-tab', { hasText: t });
	const body = await p
		.locator('.dv-groupview', { has: active })
		.first()
		.innerText()
		.catch(() => '');
	const line = body
		.split('\n')
		.filter((l) => l.trim() && !tabs.includes(l.trim()))
		.slice(1, 3)
		.join(' | ');
	console.log(`[${t}] ${line.slice(0, 100)}`);
}
await p.screenshot({ path: `${OUT}/60-as-user-sees.png`, fullPage: false });
await b.close();
