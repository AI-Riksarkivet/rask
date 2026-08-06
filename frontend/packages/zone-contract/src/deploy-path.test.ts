import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, REPO_ROOT, zoneDirs } from './manifest';

// THE DEPLOY PATH: everything OUTSIDE the frontend source that builds, ships or routes the zones —
// turbo.json, the Makefile, the dockerfile, the chart, the CI workflow, the verification scripts.
// None of it is type-checked and none of it is linted, and all of it has broken here at least once.

const zones = zoneDirs();

describe('the Makefile builds and loads exactly the zones that exist', () => {
	// `ZONES` drives `make frontend-images` (docker build --build-arg APP=$z), `make frontend-load` and
	// `make load` (kind load docker-image lance-$z:dev). It still listed the SEVEN pre-merge zones, so
	// the first command anyone would run in a real cluster — `make images`, hence `make up` — died on
	// `bun run --cwd microfrontends/data build` and never reached lakehouse. Nothing type-checks a
	// Makefile and no CI leg builds the images, so this is the only thing that can catch it.
	const makefile = readFileSync(resolve(REPO_ROOT, 'Makefile'), 'utf8');
	// All four of make's assignment operators, not just `=` and `:=`. The Makefile declares
	// `ZONES ?= …` (conditional, so the env can override it), which `:?=` does not match — the capture
	// came back empty, `declared` was `['']`, and BOTH assertions below failed while the Makefile was
	// perfectly correct. A gate that cannot parse the file it gates reports the file as broken, which
	// is worse than not gating it: it sent this to CI as "the ZONES list is stale".
	const declared = (/^ZONES\s*[:?+]?=\s*(.+)$/m.exec(makefile)?.[1] ?? '').trim().split(/\s+/);

	it('declares a ZONES list', () => {
		expect(declared.filter(Boolean).length).toBeGreaterThan(0);
	});

	it('names every zone directory, and nothing else', () => {
		expect([...declared].sort()).toEqual([...zones].sort());
	});
});

describe('the zone image can actually install the workspace', () => {
	// `bun install --frozen-lockfile` inside .docker/frontend.dockerfile fails with "Workspace not
	// found" if a member of `workspaces` was never COPYed in — and nothing else catches it, because no
	// CI leg builds the images. It already happened: `eslint-rules` was a workspace member the
	// dockerfile never copied, so every zone image build had been broken since it was added, and it was
	// only fixed as a side effect of deleting ESLint.
	const dockerfile = readFileSync(resolve(REPO_ROOT, '.docker/frontend.dockerfile'), 'utf8');
	const copied = [...dockerfile.matchAll(/^COPY\s+(.+)$/gm)].flatMap((m) =>
		m[1]!.split(/\s+/).filter((t) => t.startsWith('frontend/')),
	);
	const { workspaces = [] } = JSON.parse(
		readFileSync(resolve(FRONTEND_ROOT, 'package.json'), 'utf8'),
	) as { workspaces?: string[] };

	it('finds the COPY lines to check', () => {
		expect(copied.length).toBeGreaterThan(2);
		expect(workspaces.length).toBeGreaterThan(0);
	});

	it.each(workspaces)('the builder copies the %s workspace', (glob) => {
		// What bun install needs from every member is its package.json — PRESENCE, not sources. The
		// dockerfile now satisfies that with a `COPY --parents frontend/./<root>/*/package.json` manifest
		// glob BEFORE the install (so one zone's edit stops invalidating every zone's install layer), and
		// copies sources per-member after. Either shape guarantees the property this test protects: a new
		// workspace member cannot be silently absent at `bun install --frozen-lockfile`. A hand-listed
		// member (the eslint-rules failure this suite exists for) would satisfy neither.
		const root = `frontend/${glob.replace(/\/\*+$/, '')}`;
		const wholesale = copied.includes(root);
		const manifestGlob = copied.some((t) =>
			t.startsWith(`${root.replace('frontend/', 'frontend/./')}/*/`),
		);
		expect(
			wholesale || manifestGlob,
			`.docker/frontend.dockerfile neither COPYs ${root} wholesale nor globs ${root}/*/package.json before bun install`,
		).toBe(true);
	});
});

describe('nothing outside the frontend still points at a moved path', () => {
	// The renames kept breaking things OUTSIDE the source tree: .docker/frontend.dockerfile pre-built
	// `packages/rask-ui`, chart/templates named `@rask/api`, and scripts/verify_cross_zone_oidc.*
	// navigated to `/data` and `/models/pipeline`. None of it is type-checked, none of it is linted,
	// and all of it is on the deploy path — the dockerfile one would have failed every zone image
	// build. A rename is not done until this passes.
	const DEAD = ['@rask/', '@lance/', 'packages/rask-ui', 'microfrontends/data'];
	const SEARCHED = [
		'Makefile',
		'.docker/frontend.dockerfile',
		'chart/templates/frontends.yaml',
		'chart/templates/ingress.yaml',
		'chart/values.yaml',
		'scripts/verify_cross_zone_oidc.sh',
		'scripts/verify_cross_zone_oidc.mjs',
		'.github/workflows/ci.yml',
		'.dagger/frontend.go',
	];

	/**
	 * The file's CODE, with comments stripped.
	 *
	 * This gate matches on NAMES, and a file that EXPLAINS why it does not use one contains the same
	 * characters as a file that uses it. Measured: `chart/templates/frontends.yaml` gained a comment
	 * reading "Deliberately ABOVE the BFF's own 25 MB guard (`@rask/api/serve-proxy`)" — a sentence
	 * documenting an unrelated limit — and this gate reported the chart as naming a retired package.
	 *
	 * Rewriting the prose would be the smaller change and the wrong one: it leaves a gate whose
	 * failure mode is "someone described the system too precisely", and the tempting fix is always to
	 * delete the explanation. The ingest plane's I3/I4 grep-gates hit this three times and landed on
	 * the same answer.
	 *
	 * `//` is stripped only when NOT preceded by `:` — otherwise every `https://` URL in a chart
	 * annotation would be truncated mid-value and the gate would stop seeing real references.
	 */
	const codeOnly = (src: string): string =>
		src
			.split('\n')
			.map((line) => line.replace(/#.*$/, '').replace(/(^|[^:])\/\/.*$/, '$1'))
			.join('\n');

	it.each(SEARCHED)('%s names no retired package or zone', (rel) => {
		const path = resolve(REPO_ROOT, rel);
		if (!existsSync(path)) return; // the file moving is a separate, visible change
		const src = codeOnly(readFileSync(path, 'utf8'));
		expect(DEAD.filter((d) => src.includes(d))).toEqual([]);
	});

	it('the comment-stripping does not blind the gate to a REAL reference', () => {
		// A gate that has never been seen to fail is indistinguishable from a broken regex, and this
		// one just grew a filter that could swallow the very thing it looks for.
		const withReal = codeOnly('image: @rask/ui\n# a comment mentioning @rask/api\n');
		expect(DEAD.filter((d) => withReal.includes(d))).toEqual(['@rask/']);

		const commentOnly = codeOnly('# only a comment mentioning @rask/api\n');
		expect(DEAD.filter((d) => commentOnly.includes(d))).toEqual([]);

		// A URL survives: stripping `//` after a colon would cut every annotation value in half.
		expect(codeOnly('  url: https://example.com/x')).toContain('https://example.com/x');
	});

	// The list above holds PACKAGE and DIRECTORY names, so a retired URL path walked straight through it —
	// including in the very file the comment names. `verify_cross_zone_oidc.sh` polled
	// `http://localhost:$ORIGIN_PORT/data` as its readiness probe; the merge made that a 404, so the probe
	// never matched, the loop fail()ed after 30 tries, and the ONE script that proves cross-zone OIDC end
	// to end could not start. That is why five dead `/data` links reached main with every gate green: the
	// artifact that would have walked into them was itself broken by the same rename.
	//
	// Only lines carrying a URL are checked, and only the pre-merge zone roots. `/data` as a filesystem
	// path or a values key is none of this gate's business, and a false positive here is how a gate gets
	// deleted rather than fixed.
	const DEAD_URL_PATHS = ['data', 'lineage', 'models', 'admin'];
	it.each(SEARCHED)('%s drives no retired ORIGIN path', (rel) => {
		const path = resolve(REPO_ROOT, rel);
		if (!existsSync(path)) return;
		const offences: string[] = [];
		readFileSync(path, 'utf8')
			.split('\n')
			.forEach((line, i) => {
				if (!line.includes('http')) return;
				if (line.trimStart().startsWith('#')) return; // prose, not a request
				for (const dead of DEAD_URL_PATHS) {
					// A URL path segment right after the authority: `…:$PORT/data`, `…example.com/lineage`.
					if (new RegExp(`https?://[^/\\s"']+/${dead}(?=["'/?\\s]|$)`).test(line)) {
						offences.push(
							`${rel}:${i + 1} requests /${dead} — an area of /lakehouse since the merge`,
						);
					}
				}
			});
		expect(offences).toEqual([]);
	});
});
