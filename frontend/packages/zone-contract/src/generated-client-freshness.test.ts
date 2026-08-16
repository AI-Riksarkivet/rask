/**
 * The generated API clients must not fall behind the specs they are generated FROM.
 *
 * Nothing checked this. `gen:types` appears in no CI job, no Makefile target, no pre-commit hook and
 * no turbo task — it is a script someone is expected to remember. `lineage.ts` was last regenerated
 * 2026-07-28 and the spec moved in three commits after it (206fec72, 0922124e, 57d255c7), so the
 * client shipped for 19 days missing `RunStatus.source_run_id` and the `dapr-caller-app-id` header on
 * 27 operations. Found 2026-08-16 by an audit, not by a gate.
 *
 * The failure mode is why this is worth a test rather than a note: a missing field does not break the
 * build. The TS compiles, the property is simply absent from the type, and the surfaces that would
 * have read it render blanks — so the estate's typed boundary silently stops describing the API it
 * claims to describe. The Python half of the same chain IS gated byte-exact (`dagger call openapi`,
 * `make openapi-check`); only the hop into TypeScript was unguarded.
 *
 * WHAT THIS DOES NOT DO. It does not run `openapi-typescript` — a test that regenerates would write
 * into the working tree and would be a build, which is exactly what must not happen while a dev server
 * is serving. It compares STRUCTURE instead: every operation, schema, property and parameter the spec
 * declares must be NAMED somewhere in the generated file. That is weaker than byte-equality (a
 * reordering or a changed description slips through) and strong enough for the drift that actually
 * happens, which is a spec growing a field the client never learned about.
 *
 * The pairs are DERIVED from the `gen:types:*` scripts rather than listed here, so a third generated
 * client is covered by this gate on the day someone adds its codegen script — the same reason the
 * backup gate reads the chart's own CREATE DATABASE statements instead of naming the databases.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { REPO_ROOT } from './manifest';

const FRONTEND = resolve(REPO_ROOT, 'frontend');

/** `{ spec, out }` for every `openapi-typescript <spec> -o <out>` in the frontend's scripts. */
function generatedPairs(): { name: string; spec: string; out: string }[] {
	const pkg = JSON.parse(readFileSync(resolve(FRONTEND, 'package.json'), 'utf8')) as {
		scripts?: Record<string, string>;
	};
	const pairs: { name: string; spec: string; out: string }[] = [];
	for (const [name, script] of Object.entries(pkg.scripts ?? {})) {
		const m = /openapi-typescript\s+(\S+)\s+-o\s+(\S+)/.exec(script);
		if (m?.[1] && m[2])
			pairs.push({ name, spec: resolve(FRONTEND, m[1]), out: resolve(FRONTEND, m[2]) });
	}
	return pairs;
}

/** Every identifier the spec DECLARES, bucketed so a failure says which kind went missing. */
function declaredNames(spec: Record<string, unknown>): Record<string, Set<string>> {
	const out: Record<string, Set<string>> = {
		operation: new Set(),
		schema: new Set(),
		property: new Set(),
		parameter: new Set(),
		enumMember: new Set(),
	};

	const paths = (spec.paths ?? {}) as Record<string, Record<string, unknown>>;
	for (const item of Object.values(paths)) {
		for (const [method, op] of Object.entries(item)) {
			if (!/^(get|put|post|delete|patch|head|options|trace)$/.test(method)) continue;
			const o = op as { operationId?: string; parameters?: { name?: string; in?: string }[] };
			if (o.operationId) out.operation!.add(o.operationId);
			for (const p of o.parameters ?? []) if (p.name) out.parameter!.add(p.name);
		}
	}

	const schemas = ((spec.components as Record<string, unknown> | undefined)?.schemas ??
		{}) as Record<string, { properties?: Record<string, unknown> }>;
	for (const [name, schema] of Object.entries(schemas)) {
		out.schema!.add(name);
		for (const prop of Object.keys(schema?.properties ?? {})) out.property!.add(prop);
		// Enum MEMBERS, walked recursively — a vocabulary lives in the values, not the schema name, and
		// this is the drift that reads as "the client cannot name an event the backend publishes".
		// `ControlAction` is the live case: 30 members inside `CatalogControlEvent.properties.action`.
		collectEnums(schema, out.enumMember!);
	}
	return out;
}

/** Every string in an `enum` anywhere beneath `node`, however deeply nested. */
function collectEnums(node: unknown, into: Set<string>): void {
	if (!node || typeof node !== 'object') return;
	if (Array.isArray(node)) {
		for (const item of node) collectEnums(item, into);
		return;
	}
	for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
		if (key === 'enum' && Array.isArray(value)) {
			for (const member of value) if (typeof member === 'string') into.add(member);
		} else {
			collectEnums(value, into);
		}
	}
}

/**
 * Is `name` present in the generated source? Matched as a whole token, quoted or bare, because
 * openapi-typescript emits `source_run_id?: string` bare and `"dapr-caller-app-id"?: string` quoted
 * (the latter is not a valid bare identifier). A substring match would pass on `run_id` matching
 * `source_run_id` and make the gate vacuous in exactly the direction that matters.
 */
function names(source: string): Set<string> {
	return new Set(source.match(/[A-Za-z_$][\w$-]*/g) ?? []);
}

describe('generated API clients track their specs', () => {
	const pairs = generatedPairs();

	it('finds the codegen scripts at all', () => {
		// Without this the loop below runs zero times and the whole file passes while checking nothing —
		// the quiet failure a derived gate is most exposed to.
		expect(pairs.map((p) => p.name).sort()).toEqual(['gen:types:catalog', 'gen:types:lineage']);
	});

	for (const pair of pairs) {
		describe(pair.name, () => {
			const spec = JSON.parse(readFileSync(pair.spec, 'utf8')) as Record<string, unknown>;
			const declared = declaredNames(spec);
			const present = names(readFileSync(pair.out, 'utf8'));

			it('the spec declares something to check', () => {
				// Same anti-vacuity guard, one level down: an empty or unparsed spec would satisfy every
				// assertion below by having nothing to assert.
				expect(declared.operation!.size).toBeGreaterThan(10);
				expect(declared.schema!.size).toBeGreaterThan(10);
			});

			it('carries every enum member the spec declares', () => {
				// Matched as a quoted literal, not a bare token: openapi-typescript emits
				// `action: "grant_added" | "grant_revoked" | …`, and an enum VALUE — unlike an operation
				// or property name — is under no obligation to be a valid identifier. Today every
				// ControlAction member happens to be snake_case and would match the bare-token test by
				// luck; the first value containing a dot or a space would silently stop being checked.
				const source = readFileSync(pair.out, 'utf8');
				const missing = [...declared.enumMember!].filter((m) => !source.includes(`"${m}"`)).sort();
				expect(
					missing,
					missing.length
						? `${pair.out.replace(REPO_ROOT + '/', '')} is missing ${missing.length} enum member(s): ` +
								`${missing.slice(0, 12).join(', ')}${missing.length > 12 ? ', …' : ''}\n\n` +
								`A vocabulary the backend publishes that the client cannot NAME. Regenerate:\n` +
								`    bun --cwd=frontend run ${pair.name}`
						: undefined,
				).toEqual([]);
			});

			for (const kind of ['operation', 'schema', 'property', 'parameter'] as const) {
				it(`carries every ${kind} the spec declares`, () => {
					const missing = [...declared[kind]!].filter((n) => !present.has(n)).sort();
					expect(
						missing,
						missing.length
							? `${pair.out.replace(REPO_ROOT + '/', '')} is missing ${missing.length} ${kind}(s) that ` +
									`${pair.spec.replace(REPO_ROOT + '/', '')} declares: ${missing.slice(0, 12).join(', ')}` +
									`${missing.length > 12 ? ', …' : ''}\n\n` +
									`The generated client has fallen behind its spec. Regenerate it:\n` +
									`    bun --cwd=frontend run ${pair.name}\n\n` +
									`If the spec itself is what is stale, regenerate that first with \`make openapi\`.`
							: undefined,
					).toEqual([]);
				});
			}
		});
	}
});
