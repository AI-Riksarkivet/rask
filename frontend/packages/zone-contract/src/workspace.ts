import { existsSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { FRONTEND_ROOT } from './manifest';

/**
 * Filesystem facts about the workspace that more than one gate asks for.
 *
 * These used to be private helpers inside manifest.test.ts, which is why that file grew to nine
 * unrelated concerns: a gate that needed `workspacePackages()` had to be written next to it. Exported
 * here so a gate can live in the file named for what it checks.
 */

/** Every workspace package, as a frontend-root-relative directory (`packages/api`, …). */
export function workspacePackages(): string[] {
	return ['packages', 'microfrontends'].flatMap((dir) =>
		readdirSync(resolve(FRONTEND_ROOT, dir), { withFileTypes: true })
			.filter(
				(e) => e.isDirectory() && existsSync(resolve(FRONTEND_ROOT, dir, e.name, 'package.json')),
			)
			.map((e) => `${dir}/${e.name}`),
	);
}

/** A zone's e2e helper servers (mock backends) — the other place a port literal hides. */
export function e2eServers(zone: string): string[] {
	const dir = resolve(FRONTEND_ROOT, `microfrontends/${zone}/e2e`);
	if (!existsSync(dir)) return [];
	return readdirSync(dir, { recursive: true, encoding: 'utf8' })
		.filter((f) => f.endsWith('.ts') && !f.endsWith('.spec.ts'))
		.map((f) => `e2e/${f}`);
}
