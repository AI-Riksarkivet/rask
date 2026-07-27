import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, hasLintableFiles } from './manifest';
import { workspacePackages } from './workspace';

// THE TOOLCHAIN: one linter, one formatter, one config each, invoked identically by every package.
// Nothing here is about zones — it is about the workspace not growing a second opinion about style.

describe('there is exactly one config per tool, at the workspace root', () => {
	// media and annotator arrived from a standalone repo that already used the oxc toolchain, and
	// carried their own .oxfmtrc.json / .oxlintrc.json in. Both tools resolve the NEAREST config
	// upward, so those files silently overrode the root config for that zone the moment oxlint and
	// oxfmt were switched on — media got 80-column double-quoted output and a different plugin set,
	// and nothing reported it. A per-package config must be a deliberate act, not a leftover.
	// The prettier / eslint entries stay in this list AFTER both tools were removed: the failure mode
	// is a file reappearing (a generator, a copied package, an editor plugin) and configuring a tool
	// nothing runs, which reads as coverage that does not exist.
	it.each([
		'.oxlintrc.json',
		'.oxfmtrc.json',
		'.prettierrc',
		'.prettierrc.json',
		'eslint.config.js',
	])('%s exists only at the frontend root', (name) => {
		const strays: string[] = [];
		for (const dir of ['packages', 'microfrontends']) {
			for (const pkg of readdirSync(resolve(FRONTEND_ROOT, dir), { withFileTypes: true })) {
				if (!pkg.isDirectory()) continue;
				const found = readdirSync(resolve(FRONTEND_ROOT, dir, pkg.name));
				if (found.includes(name)) strays.push(`${dir}/${pkg.name}/${name}`);
			}
		}
		expect(strays).toEqual([]);
	});

	// Two tools, two commands, everywhere. rsvelte-fmt formats `.svelte` in process and delegates every
	// other extension to oxfmt, and oxlint hosts the Svelte rules through @rsvelte/oxlint-plugin — so a
	// package that still spawns eslint or prettier is running a toolchain that is no longer installed,
	// and `--no-error-on-unmatched-pattern` would make that look like a pass.
	// `--no-error-on-unmatched-pattern` is allowed for EXACTLY the packages that have no lintable file
	// (a config-only package like @rask/config ships two JSON files; plain `oxlint .` exits 1 there, so
	// without this the package must drop the script — the omission the next assertion forbids). It is
	// FORBIDDEN for every package that does have source, which is the masking case the comment above
	// warns about: the filesystem decides which command a package must declare, so the flag can never
	// hide a zone whose paths stopped matching.
	const lintFor = (pkg: string) =>
		hasLintableFiles(resolve(FRONTEND_ROOT, pkg))
			? 'oxlint .'
			: 'oxlint --no-error-on-unmatched-pattern .';
	const EXPECTED: Record<string, string> = {
		fmt: 'rsvelte-fmt .',
		'fmt:check': 'rsvelte-fmt --check .',
	};
	it.each(workspacePackages())('%s runs the one lint and format command', (pkg) => {
		const { scripts = {} } = JSON.parse(
			readFileSync(resolve(FRONTEND_ROOT, pkg, 'package.json'), 'utf8'),
		) as { scripts?: Record<string, string> };
		// REQUIRED, not just "correct if present". Skipping an absent task made the gate vacuous in
		// the direction that actually happens: a package does not drift to a WRONG command, it ships
		// with NO lint/fmt scripts and turbo then has nothing to run for it — silently outside the
		// toolchain while every gate stays green. @rask/config was exactly that (found 2026-07-25:
		// two JSON files, no scripts, never format-checked). A package with genuinely nothing to
		// check still declares them; oxlint/rsvelte-fmt over zero matching files is free.
		for (const [task, expected] of Object.entries({ ...EXPECTED, lint: lintFor(pkg) })) {
			expect(scripts[task], `${pkg} is missing the shared ${task} script`).toBe(expected);
		}
		for (const [task, script] of Object.entries(scripts)) {
			expect(script, `${pkg} ${task} still invokes a removed tool`).not.toMatch(
				/\b(eslint|prettier)\b/,
			);
		}
	});
});

/** A zone's e2e helper servers (mock backends) — the other place a port literal hides. */
