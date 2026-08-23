import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { FRONTEND_ROOT, REPO_ROOT, zoneDirs } from './manifest';

/**
 * No zone test may wait for `networkidle`. It can never fire again, and it fails by TIMING OUT.
 *
 * Every zone's shell now holds a live `query.live` stream open for the notification bell, so these apps
 * have no idle network by design. A `waitUntil: 'networkidle'` or `waitForLoadState('networkidle')` sits
 * until its own timeout on every navigation and then reports a failure that looks like the product hanging.
 *
 * That is exactly how it presented: mounting the bell in the remaining three zones turned one home-zone
 * spec red with `Error: waitForLoadState: Test timeout of 30000ms exceeded`, and nine more waits sat in the
 * media zone's live drives waiting to do the same on the next run. The lakehouse zone had already hit this
 * once and left the reason in a comment — a comment no other zone could be failed by.
 *
 * Playwright discourages `networkidle` for its own reasons. Here it is not a smell, it is a contradiction
 * with the architecture, so it is a gate: wait on the ELEMENT the test is about to act on, or assert the
 * effect of the action and retry (`expect(async () => {...}).toPass()`).
 */
const FORBIDDEN = /networkidle/;

function testFiles(dir: string, acc: string[] = [], anyScript = false): string[] {
	if (!existsSync(dir)) return acc;
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.name === 'node_modules') continue;
		const path = join(dir, entry.name);
		if (entry.isDirectory()) testFiles(path, acc, anyScript);
		// A zone's `e2e/` holds ordinary source beside its specs, so there the name pattern is the
		// filter. A dedicated browser-driving tree does not: EVERY script in it drives a browser, and
		// requiring `.spec`/`.e2e` in the name is how `@rask/ui`'s `harness/drive.mjs` kept a
		// `networkidle` the zone-shaped matcher could never see.
		else if (
			anyScript
				? /\.(ts|mjs|js)$/.test(entry.name)
				: /\.(spec|test|e2e)\.(ts|mjs|js)$/.test(entry.name)
		)
			acc.push(path);
	}
	return acc;
}

/** A line that only TALKS about the ban (this file's own rule, restated in a zone) is not a use of it. */
const isComment = (line: string) => /^\s*(\/\/|\*|\/\*)/.test(line);

/**
 * Browser-driving trees OUTSIDE `microfrontends/<zone>/e2e`, which the per-zone loop cannot see.
 *
 * The gate scanned only the zone suites, and the estate has two other places that drive a browser:
 * the standalone Playwright project at `tests/e2e` (its own lockfile, run by `make e2e`) and
 * `@rask/ui`'s Storybook harness. Both held a surviving `networkidle` — `mfe.spec.ts` on the very
 * navigation whose result it then asserts. Three of the seven zones ship no `e2e/` directory at all,
 * so for them the per-zone loop was vacuous too; these roots are where the remaining risk actually
 * lived.
 */
const EXTRA_ROOTS = [
	join(REPO_ROOT, 'tests', 'e2e'),
	join(FRONTEND_ROOT, 'packages', 'ui', 'harness'),
];

describe('no zone test waits for an idle network', () => {
	for (const zone of zoneDirs()) {
		it(`${zone}`, () => {
			const offenders = testFiles(join(FRONTEND_ROOT, 'microfrontends', zone, 'e2e'))
				.flatMap((file) =>
					readFileSync(file, 'utf8')
						.split('\n')
						.map((line, i) => ({ file, line, n: i + 1 }))
						.filter(({ line }) => FORBIDDEN.test(line) && !isComment(line)),
				)
				.map(({ file, line, n }) => `${file.replace(FRONTEND_ROOT, '')}:${n}: ${line.trim()}`);

			expect(
				offenders,
				'the shell holds a live stream open in every zone, so `networkidle` cannot fire — this wait ' +
					'will time out and report the product as hanging. Wait on the element instead:\n  ' +
					offenders.join('\n  '),
			).toEqual([]);
		});
	}

	for (const root of EXTRA_ROOTS) {
		it(`${root.replace(REPO_ROOT, '')}`, () => {
			const offenders = testFiles(root, [], true)
				.flatMap((file) =>
					readFileSync(file, 'utf8')
						.split('\n')
						.map((line, i) => ({ file, line, n: i + 1 }))
						.filter(({ line }) => FORBIDDEN.test(line) && !isComment(line)),
				)
				.map(({ file, line, n }) => `${file.replace(REPO_ROOT, '')}:${n}: ${line.trim()}`);

			expect(
				offenders,
				'this tree drives a real browser against the shell, which holds a live stream open, so ' +
					'`networkidle` cannot fire — the wait times out and reports the product as hanging:\n  ' +
					offenders.join('\n  '),
			).toEqual([]);
		});
	}
});
