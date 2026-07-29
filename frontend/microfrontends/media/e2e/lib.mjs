/**
 * Shared E2E plumbing: chromium resolution (WebGPU-capable, both playwright layouts),
 * launch args, the PASS/FAIL collector, and common preconditions. Each suite composes
 * these; keep suite-specific gestures in the suites.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

// The composition origin: @rask/zone-contract's dev proxy (`bun run dev` at the frontend
// root), which path-routes every zone prefix off ONE origin exactly as the Ingress does — so
// these suites drive the real split topology rather than a single zone's port.
export const BASE = process.env.E2E_BASE ?? 'http://127.0.0.1:5200';
// API goes through the same origin the app uses: /media/api/* is the media zone's own BFF,
// which bearer-forwards to viewer/search/annotator.
export const API = process.env.E2E_API ?? 'http://127.0.0.1:5200';
export const KEY = process.env.E2E_KEY ?? 'fe00cd746463ad2c/0/19';
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../..');

function chromePath() {
	if (process.env.E2E_CHROME) return process.env.E2E_CHROME;
	const cache = path.join(process.env.HOME ?? '', '.cache/ms-playwright');
	const builds = existsSync(cache)
		? readdirSync(cache)
				.filter((d) => /^chromium-\d+$/.test(d))
				.sort((a, b) => Number(a.split('-')[1]) - Number(b.split('-')[1])) // numeric, not lexical
		: [];
	for (const b of builds.reverse()) {
		// The binary layout changed across Playwright versions: chrome-linux64/ (new) vs chrome-linux/ (old).
		for (const layout of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
			const p = path.join(cache, b, layout);
			if (existsSync(p)) return p;
		}
	}
	throw new Error('no chromium found — set E2E_CHROME or `npx playwright-core install chromium`');
}

/** Launch new-headless Chromium with WebGPU/Vulkan live. */
export function launchBrowser() {
	return chromium.launch({
		executablePath: chromePath(),
		headless: false, // new-headless below keeps WebGPU/Vulkan available
		args: [
			'--headless=new',
			'--no-sandbox',
			'--enable-unsafe-webgpu',
			'--enable-features=Vulkan',
			'--use-angle=vulkan',
			'--ignore-gpu-blocklist',
		],
	});
}

/** Reset the demo annotations table (deterministic runs; leaves the demo clean). */
export function seed() {
	execFileSync('make', ['seed-annotations', 'DB=transcripts_v2.lance'], {
		cwd: REPO,
		stdio: 'pipe',
	});
}

/** Assert backend + dev server (+ the demo unit) are reachable; exit 2 otherwise. */
export async function assertPreconditions(extra = []) {
	const checks = [
		['backend', `${API}/api/datasets`],
		['dev server', `${BASE}/`],
		['demo unit (E2E_KEY)', `${API}/api/annotations/${KEY}`],
		...extra,
	];
	for (const [name, url] of checks) {
		const res = await fetch(url).catch(() => null);
		if (!res?.ok) {
			console.error(`PRECONDITION FAILED: ${name} not reachable at ${url}`);
			process.exit(2);
		}
	}
}

/** PASS/FAIL collector — `finish(crashed)` prints the summary and exits. */
export function collector(suiteName) {
	const results = [];
	return {
		ok(name, pass, detail = '') {
			results.push({ name, pass });
			console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
		},
		finish(crashed = null) {
			const passed = results.filter((r) => r.pass).length;
			console.log(
				`\n=== ${suiteName}: ${passed}/${results.length} passed${crashed ? ' (CRASHED before completing)' : ''} ===`,
			);
			process.exit(!crashed && passed === results.length ? 0 : 1);
		},
	};
}
