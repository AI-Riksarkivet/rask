/**
 * No zone reads a SECRET out of the environment — only the PATH to one.
 *
 * [[XC-001]], [[LH-160]]. The estate's rule is the owner's, verbatim: "Never secret through envs.
 * Either from ESO, secret store dapr and STS for zero trust." A zone carries no Dapr sidecar, so its
 * sanctioned delivery is an ESO-managed Secret taken as a MOUNTED FILE.
 *
 * THIS GATE EXISTS BECAUSE THE FIRST ATTEMPT MISSED NINE CALL SITES. `LINEAGE_SERVICE_TOKEN` was
 * changed in `@rask/api`'s `makeLineageProxy` and removed from the chart, and that looked complete —
 * but seven zones never call that factory. They read `env.LINEAGE_SERVICE_TOKEN` directly in their own
 * `.remote.ts` files, so removing the env var left all nine reading `undefined`. The zone still served
 * 200 at its root: the token is only consulted on a lineage call, so the break was silent, which is
 * exactly the failure this estate keeps paying for.
 *
 * A GREP, NOT A TYPE. There is no type that can say "this env key holds a secret" — the env record is
 * `Record<string, string>` by construction — so the only thing that can hold this line is a check over
 * the source. Scoped to the names that ARE secrets rather than to a pattern, because a heuristic over
 * "looks like a secret" would fire on `OIDC_ISSUER` and be deleted within a week.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const FRONTEND = join(fileURLToPath(new URL('.', import.meta.url)), '..', '..', '..');

/** Secrets that reach a zone as a file. The `_FILE` env naming the PATH is fine — a path is not a secret. */
const SECRETS_DELIVERED_AS_FILES = ['LINEAGE_SERVICE_TOKEN'];

function sourceFiles(dir: string, out: string[] = []): string[] {
	for (const entry of readdirSync(dir)) {
		if (
			entry === 'node_modules' ||
			entry === '.svelte-kit' ||
			entry === 'dist' ||
			entry === 'build'
		)
			continue;
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) sourceFiles(path, out);
		else if (entry.endsWith('.ts') || entry.endsWith('.svelte')) out.push(path);
	}
	return out;
}

describe('secrets reach a zone as a file, never as an env value', () => {
	const files = sourceFiles(join(FRONTEND, 'microfrontends')).concat(
		sourceFiles(join(FRONTEND, 'packages')),
	);

	it('finds sources to check', () => {
		// Without this the whole suite passes by walking an empty tree.
		expect(files.length).toBeGreaterThan(100);
	});

	for (const name of SECRETS_DELIVERED_AS_FILES) {
		it(`no source reads env.${name} as a value`, () => {
			const offenders = files.filter((f) => {
				if (f.endsWith('secret-from-file.test.ts')) return false;
				const text = readFileSync(f, 'utf8');
				// `env.NAME` but NOT `env.NAME_FILE` — the latter is the sanctioned path variable.
				return new RegExp(`env\\.${name}(?![A-Z_])`).test(text);
			});

			expect(offenders.map((f) => f.slice(FRONTEND.length + 1))).toEqual([]);
		});
	}
});
