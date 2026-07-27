// The zone manifest, read back out of the four files that declare it. Pure parsing — the assertions
// live in manifest.test.ts. Deliberately regex/JSON based rather than importing the configs: the point
// is to read what is WRITTEN in each file, so a value that only agrees because one file imports the
// other still counts as a disagreement.
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** frontend/ — this package lives at frontend/packages/zone-contract/src. */
export const FRONTEND_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
/** The repo root, one level above frontend/ — where chart/ lives. */
export const REPO_ROOT = resolve(FRONTEND_ROOT, '..');

const read = (path: string) => readFileSync(resolve(FRONTEND_ROOT, path), 'utf8');

export interface RoutingConfigApp {
	development?: { local?: { port?: number } };
	routing?: { paths: string[] }[];
}

export interface RoutingConfig {
	applications: Record<string, RoutingConfigApp>;
}

/** `microfrontends/home/microfrontends.json` — the default app ships the routing config. */
export function routingConfig(): RoutingConfig {
	return JSON.parse(read('microfrontends/home/microfrontends.json')) as RoutingConfig;
}

/** The zone directories that actually exist under microfrontends — the ground truth every other
 *  declaration is checked against.
 *
 *  A zone is a directory with a `package.json`, which is what makes it a bun workspace member and a
 *  turbo package — the same rule the rest of the toolchain resolves by. Bare `isDirectory()` is NOT
 *  that rule: deleting a zone leaves its gitignored build output behind (`build/`, `.svelte-kit/`,
 *  `node_modules/`, `.turbo/`), so the four pre-merge zones — admin, data, lineage, models — survive
 *  as husks in every working tree that ever built them. `git status` is clean, a fresh checkout looks
 *  fine, and the CI cache hides it too. Counting those husks as zones failed 39 tests across all four
 *  gate files at once, and `.svelte-kit/output/client` is still there so even the budget gate found
 *  something to weigh. A gate that fires on leftover build output is worse than no gate: the fix
 *  people reach for is deleting the gate. */
export function zoneDirs(): string[] {
	return readdirSync(resolve(FRONTEND_ROOT, 'microfrontends'), { withFileTypes: true })
		.filter((e) => e.isDirectory())
		.map((e) => e.name)
		.filter((name) => existsSync(resolve(FRONTEND_ROOT, 'microfrontends', name, 'package.json')))
		.sort();
}

/** Does this package hold anything oxlint would lint? Decides which lint command it must declare:
 *  plain `oxlint .` exits 1 on a package with no matching file, so a config-only package (two JSON
 *  files) needs `--no-error-on-unmatched-pattern` — and a package WITH source must not use that flag,
 *  or a zone whose paths stopped matching would pass silently. Build output is skipped: it is
 *  gitignored, absent in CI, and would otherwise make the answer depend on whether someone had run a
 *  build. */
export function hasLintableFiles(pkgDir: string): boolean {
	const SKIP = new Set(['node_modules', 'build', '.svelte-kit', 'dist', '.turbo']);
	const walk = (dir: string): boolean =>
		readdirSync(dir, { withFileTypes: true }).some((e) =>
			e.isDirectory()
				? !SKIP.has(e.name) && walk(resolve(dir, e.name))
				: /\.(ts|js|mjs|svelte)$/.test(e.name),
		);
	return existsSync(pkgDir) && walk(pkgDir);
}

/** The `paths.base` a zone serves under, from its svelte.config.js. `''` for the catch-all zone. */
export function svelteBase(zone: string): string {
	const src = read(`microfrontends/${zone}/svelte.config.js`);
	return /paths:\s*\{[^}]*base:\s*['"]([^'"]*)['"]/.exec(src)?.[1] ?? '';
}

/** The dev port a zone's vite.config.ts binds, and whether it binds it strictly. */
export function vitePort(zone: string): { port: number | null; strict: boolean } {
	const src = read(`microfrontends/${zone}/vite.config.ts`);
	const port = /^\s*port:\s*(\d+)/m.exec(src)?.[1];
	return { port: port ? Number(port) : null, strict: /strictPort:\s*true/.test(src) };
}

/** A zone's package name. Turborepo resolves `microfrontends.json` application keys against WORKSPACE
 *  PACKAGE names, so a zone whose package is scoped differently than its key is silently unroutable —
 *  `turbo boundaries` warns, nothing fails. */
export function packageName(zone: string): string {
	return JSON.parse(read(`microfrontends/${zone}/package.json`)).name as string;
}

/** A zone's `dev` script, so we can assert the port is not ALSO declared there (two sources of truth). */
export function devScript(zone: string): string {
	return JSON.parse(read(`microfrontends/${zone}/package.json`)).scripts?.dev ?? '';
}

export interface ChartApp {
	name: string;
	path: string | null;
	catchAll: boolean;
}

/** `chart/values.yaml` → `frontend.apps`. Parsed from the flow-mapping list entries so this stays a
 *  dependency-free read (no YAML parser in the frontend workspace). */
export function chartApps(): ChartApp[] {
	const yaml = readFileSync(resolve(REPO_ROOT, 'chart/values.yaml'), 'utf8');
	const block = /^\s{2}apps:\s*$/m.exec(yaml);
	if (!block) return [];
	const rest = yaml.slice(block.index + block[0].length);
	const end = /^\s{0,2}\w/m.exec(rest);
	const body = end ? rest.slice(0, end.index) : rest;
	return [...body.matchAll(/^\s*-\s*\{([^}]*)\}/gm)].map((m) => {
		const entry = m[1] ?? '';
		return {
			name: /name:\s*([\w-]+)/.exec(entry)?.[1] ?? '',
			path: /path:\s*(\/[\w-]*)/.exec(entry)?.[1] ?? null,
			catchAll: /catchAll:\s*true/.test(entry),
		};
	});
}
