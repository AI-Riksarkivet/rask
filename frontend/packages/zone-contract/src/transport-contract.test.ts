/**
 * THREE cross-zone invariants that no single zone can assert, and that nobody owns.
 *
 * WHY THIS FILE EXISTS. On 2026-08-06 two Claude sessions worked this estate at once and collided
 * three times in one merge. Every collision was a rule that was already WRITTEN DOWN and enforced by
 * nothing:
 *
 *   * `liveRead` was extracted TWICE, independently — into `@rask/api/live` (converging three
 *     divergent copies, 274 lines) and into `@rask/ui/live-read`. Both were reasonable. Neither
 *     session could see the other.
 *   * `createWarehouse`'s valibot schema is declared in TWO zones. Main added a required `protected`
 *     field to the catalog body; both copies went stale, and the SAME one-line fix had to be applied
 *     twice, an hour apart.
 *   * The transport ruling ("remote functions for values, `+server.ts` for bytes") lived in a skill
 *     and a doc, so a new BFF JSON route would have been caught by review or not at all.
 *
 * The question "which session should audit this?" has no answer — whoever asks last wins, and
 * neither can see the other's tree. The answer is that NEITHER should: the test owns it.
 *
 * Every gate here is written so its floor is visible. A counter that drifts to zero makes the
 * assertions above it vacuous while the suite stays green, which is the quieter failure — see
 * `nav-truth.test.ts`'s `ALL.length > 30` guard for the same reasoning.
 */

import { existsSync, readFileSync } from 'node:fs';
import { globSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT } from './manifest';

const read = (p: string) => readFileSync(p, 'utf8');
const rel = (p: string) => p.replace(`${FRONTEND_ROOT}/`, '');
/** `microfrontends/<zone>/src/...` → `<zone>`. Index 1, not 2: index 2 is `src`, and taking it
 *  collapsed every zone onto one key — which made the cross-zone comparisons below compare a zone
 *  with itself and pass vacuously. Caught only because the canary assertion went red. */
const zoneOf = (p: string) => rel(p).split('/')[1] ?? '';

/** Files under `frontend/`, excluding build output and dependencies. */
function sources(pattern: string): string[] {
	return globSync(pattern, { cwd: FRONTEND_ROOT })
		.filter(
			(p) => !p.includes('node_modules') && !p.includes('/dist/') && !p.includes('/.svelte-kit/'),
		)
		.map((p) => resolve(FRONTEND_ROOT, p));
}

// ── 1 · ONE liveRead ────────────────────────────────────────────────────────────────────────────

describe('liveRead has exactly one implementation', () => {
	/**
	 * `liveRead` is not a helper, it is the estate's LIVENESS CONTRACT: four rules (first read
	 * unconditional, key tracked, cursor ARRIVAL is not a change, stream opened on mount) that each
	 * exist because breaking one broke a test. It replaced thirteen hand-rolled `$effect`+`setInterval`
	 * pollers.
	 *
	 * A second copy does not announce itself. Both compile, both work, and they diverge on the rule
	 * whose measurement only one of them carries — which is exactly what the three pre-consolidation
	 * copies had already done: only one still explained the open-on-mount rule.
	 */
	const IMPL = /export\s+(?:function|const)\s+liveRead\b/;

	it('is implemented in @rask/api and nowhere else', () => {
		const implementors = sources('{packages,microfrontends}/**/*.ts')
			.filter((p) => IMPL.test(read(p)))
			.map(rel)
			.sort();

		expect(implementors).toEqual(['packages/api/src/live.svelte.ts']);
	});

	it('every zone that goes live imports it from @rask/api/live', () => {
		// The per-zone `tick.svelte.ts` shim is legitimate and cannot move: `query.live` must be
		// declared INSIDE an app to get its own endpoint and reach `getRequestEvent`. What must not
		// move is the mechanism — the shim binds cursors, it does not re-implement the reader.
		const shims = sources('microfrontends/*/src/lib/live/tick.svelte.ts');
		expect(shims.length).toBeGreaterThan(2); // floor: a shrinking set makes the loop below vacuous

		for (const shim of shims) {
			expect(read(shim), `${rel(shim)} must take liveRead from @rask/api/live`).toMatch(
				/from\s+'@rask\/api\/live'/,
			);
		}
	});

	it('no zone re-implements it under a different name in its own live/ directory', () => {
		// The rename dodge: a local `useLive`/`poll`/`livePoll` is the same duplication wearing a
		// different label, and the name is the only thing the gate above keys on.
		const DODGE = /export\s+(?:function|const)\s+(usePoll|livePoll|useLive|pollRead)\b/;
		const offenders = sources('microfrontends/*/src/lib/live/*.ts')
			.filter((p) => DODGE.test(read(p)))
			.map(rel);

		expect(offenders).toEqual([]);
	});
});

// ── 2 · transport matches the payload KIND ──────────────────────────────────────────────────────

describe('transport matches the payload kind', () => {
	/**
	 * The ruling (2026-08-03): typed app VALUES ride remote functions; TABULAR/BINARY payloads ride
	 * Arrow IPC or raw bytes on `+server.ts`. Both halves are idiomatic SvelteKit — `+server.ts` is
	 * the framework's own tool for non-HTML resources, not a legacy dialect.
	 *
	 * The rule is measured, not aesthetic. devalue DOES carry ArrayBuffer/TypedArrays — as base64
	 * inside the payload string — which costs +33% on the wire, triple-buffers the whole payload
	 * (bytes → base64 → bytes, no streaming), and loses content-type, ETags and ranges.
	 *
	 * It cuts BOTH ways, and the reverse mistake is the one that hides: a JSON route carrying bulk
	 * tabular rows should be PROMOTED to Arrow, not converted to a remote function.
	 */

	it('no remote function returns Arrow or raw bytes', () => {
		const BYTES = /\b(ArrayBuffer|Uint8Array|tableToIPC|RecordBatch|arrayBuffer\(\))/;
		const offenders = sources('microfrontends/*/src/**/*.remote.ts')
			.filter((p) => BYTES.test(read(p)))
			.map(rel);

		expect(
			offenders,
			'a remote function carrying bytes base64-encodes them into the payload — promote it to a `+server.ts` Arrow route',
		).toEqual([]);
	});

	it('the BFF JSON residual only ever SHRINKS', () => {
		// `requestJSON` is the BFF's JSON helper. Six zones are converged and hold zero call sites;
		// the lakehouse is the residual. Pinning the count as a CEILING is what makes convergence
		// irreversible without a visible edit: a new BFF JSON route anywhere fails here, and when a
		// residual is converged the number comes down with it.
		const counts = new Map<string, number>();
		for (const p of sources('microfrontends/*/src/**/*.ts')) {
			const n = (read(p).match(/\brequestJSON\b/g) ?? []).length;
			if (n > 0) counts.set(zoneOf(p), (counts.get(zoneOf(p)) ?? 0) + n);
		}

		// The six converged zones must stay at zero — that is the half worth protecting hardest.
		for (const zone of ['home', 'explorer', 'annotator', 'compute', 'models', 'studio']) {
			expect(
				counts.get(zone) ?? 0,
				`${zone} was converged off requestJSON — do not add a BFF JSON route`,
			).toBe(0);
		}

		// The lakehouse's residual is allowed to exist and NOT allowed to grow. If this fails because
		// you converged one, lower the number in the same commit.
		expect(counts.get('lakehouse') ?? 0).toBeLessThanOrEqual(13);
	});

	it('the OIDC endpoints stay on +server.ts', () => {
		// Permanently a BFF route, and not an oversight: these are redirect FLOWS, not function calls.
		// A remote function cannot 302 a browser to an identity provider.
		// Checked by PATH rather than by glob: `+server.ts` leads with `+`, which several glob
		// implementations read as an extglob quantifier, and the pattern then matches nothing — a gate
		// that silently asserts "zero routes is fine" is worse than no gate.
		const routes = ['login', 'callback', 'logout'].map((r) =>
			resolve(FRONTEND_ROOT, `microfrontends/home/src/routes/auth/${r}/+server.ts`),
		);
		const missing = routes.filter((p) => !existsSync(p)).map(rel);

		expect(
			missing,
			'an OIDC BFF route vanished — a remote function cannot serve a redirect flow',
		).toEqual([]);
	});
});

// ── 3 · one schema per remote-function NAME ─────────────────────────────────────────────────────

describe('a remote function declared in two zones declares the same input schema', () => {
	/**
	 * Ten remote-function names exist in more than one zone, and that is CORRECT rather than
	 * duplication: `command`/`query.live` must be declared inside an app to get its own endpoint, so
	 * the per-zone shim is structural. `lineageFeed` lives in all seven zones for that reason.
	 *
	 * What is NOT structural is the SCHEMA. `createWarehouse` is declared in `home` and `lakehouse`
	 * with a hand-copied valibot object each. Main's #73 made `protected` required on
	 * `CreateWarehouseBody`; both copies went stale, both broke identically, and the same one-line fix
	 * landed twice an hour apart — the second only because a typecheck happened to run over both zones.
	 *
	 * The failure is also badly located: valibot's inferred argument type silently loses the field and
	 * TypeScript reports "no overload matches this call" on `command(...)`, naming schema internals,
	 * with the actual missing property mentioned only at a CALL SITE in a different file.
	 *
	 * The real fix is hoisting these schemas into `@rask/api` beside the generated types they mirror.
	 * Until then this gate makes the drift loud at the moment it is introduced rather than at the next
	 * unrelated merge.
	 */

	/**
	 * `export const NAME = query|command(v.object({ … }), …)` → the set of TOP-LEVEL FIELD NAMES.
	 *
	 * Fields, not schema TEXT. Comparing text was the first attempt and it was wrong in both
	 * directions: it reported `checkAccess` as a violation because one zone passes a NAMED
	 * `SubjectSchema` while the other inlines the same object — and a named, shared schema is the
	 * outcome this gate is trying to encourage, not a defect. It would equally have missed a real
	 * drift hidden behind reordered keys or a differing comment.
	 *
	 * A field SET is what actually broke: `protected` was absent from both copies and required by the
	 * body type. Order-insensitive, comment-insensitive, and it says the missing name out loud.
	 *
	 * A schema passed by REFERENCE yields no fields and is skipped — deliberately. It is either shared
	 * already or defined in a sibling module, and either way this gate has nothing to add.
	 */
	function fieldsByName(src: string): Map<string, string> {
		const out = new Map<string, string>();
		const decl =
			/export\s+const\s+([a-zA-Z_]\w*)\s*=\s*(?:query|command)(?:\.live)?\s*\(\s*v\.object\(\{([\s\S]*?)\}\)\s*,/g;
		for (const m of src.matchAll(decl)) {
			const [, name, body] = m;
			if (!name || !body) continue;
			const stripped = body.replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
			// Top level only: a nested `v.object({...})`'s keys sit at a deeper brace depth and are not
			// this schema's fields. Tracked by depth rather than by regex, which cannot count braces.
			const fields: string[] = [];
			let depth = 0;
			for (const line of stripped.split('\n')) {
				const key = depth === 0 ? /^\s*([a-zA-Z_]\w*)\s*:/.exec(line) : null;
				if (key?.[1]) fields.push(key[1]);
				depth += (line.match(/[({[]/g) ?? []).length - (line.match(/[)}\]]/g) ?? []).length;
			}
			// JOINED, not the array: a Set of arrays dedupes by REFERENCE, so two identical field
			// lists counted as two distinct schemas and every shared name failed.
			out.set(name, fields.sort().join(','));
		}
		return out;
	}

	/**
	 * Identity is NAME + UPSTREAM PATH, not name alone.
	 *
	 * A name collision is not drift. `createProject` exists in `explorer` and `home` and they are
	 * genuinely different operations — explorer POSTs `/projects` on the projects API with
	 * `{tenant, slug, title}`, home POSTs `/v1/projects` on the catalog with `{id, name}`. Keying on
	 * the name alone reported that as a violation, which would have pushed someone to "reconcile" two
	 * schemas that were each already right.
	 *
	 * (The collision is its own smell — two remote functions with one name against two APIs is how a
	 * fix lands in the wrong zone — but it is a naming problem, not a contract breach, and this gate
	 * is not the place to fail the build over it.)
	 */
	function upstreamPath(src: string, name: string): string {
		const body = src.slice(src.indexOf(`export const ${name} =`));
		return /['`](\/[\w/.$-]*)['`]/.exec(body.slice(0, 600))?.[1] ?? '';
	}

	const byName = new Map<string, Map<string, string>>();
	for (const p of sources('microfrontends/*/src/**/*.remote.ts')) {
		const src = read(p);
		for (const [name, fields] of fieldsByName(src)) {
			const key = `${name} -> ${upstreamPath(src, name)}`;
			if (!byName.has(key)) byName.set(key, new Map());
			byName.get(key)!.set(zoneOf(p), fields);
		}
	}

	const shared = [...byName.entries()].filter(([, zones]) => zones.size > 1);

	it('finds shared inline schemas at all — a parser that matches nothing passes vacuously', () => {
		// The gate's whole value is the comparison below; a regex that stopped matching would make it
		// assert nothing while staying green. `createWarehouse` is the case that motivated the file.
		expect(byName.size).toBeGreaterThan(10);
		expect([...byName.keys()].some((k) => k.startsWith('createWarehouse '))).toBe(true);
		expect(shared.length).toBeGreaterThan(0);
	});

	it.each(shared.map(([name]) => name))(
		'%s declares one schema across every zone that has it',
		(name) => {
			const zones = byName.get(name)!;
			const distinct = new Set(zones.values());

			expect(
				distinct.size,
				`${name} is declared in ${[...zones.keys()].sort().join(', ')} with ${distinct.size} DIFFERENT field sets:\n` +
					[...zones].map(([z, f]) => `  ${z}: ${f || '(none)'}`).join('\n') +
					'\n' +
					'The per-zone endpoint shim is structural; the schema is not. Hoist it into @rask/api beside the ' +
					'generated type it mirrors, or make the copies agree in this commit — a stale copy fails as an ' +
					'unreadable overload error in an unrelated file.',
			).toBe(1);
		},
	);
});
