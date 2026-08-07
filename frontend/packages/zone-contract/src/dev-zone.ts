#!/usr/bin/env bun
/**
 * ONE zone, with fake upstreams, and nothing else running.
 *
 *     make dev-zone ZONE=lakehouse
 *
 * WHY IT LIVES IN `zone-contract`, a package documented as test-only. Because the alternative was
 * worse: it started life at `frontend/scripts/`, which matches NEITHER workspace glob
 * (`microfrontends/*`, `packages/*`) — so no package owned it and therefore no `lint` or `fmt` task
 * ever saw it. That is precisely the "sits silently outside the toolchain while every gate stays
 * green" failure `toolchain.test.ts` exists to prevent, reintroduced one directory over. This package
 * already hosts dev tooling next to its gates (`src/proxy.ts` + its `dev:proxy` script), so the
 * precedent is established and the launcher now sits beside the very test that guards it.
 *
 * WHY THIS EXISTS. The estate already had everything needed to develop a single zone against fake
 * backends — seed-driven `Bun.serve` mocks, sealed dev cookies, per-zone port registries — but it
 * lived exclusively INSIDE each zone's `playwright.config.ts`. So the capability was real and
 * invisible: the documented loops were `make dev-frontends` (all 7 zones + the :3024 proxy) and
 * `make dev-micro` (the uvicorn fleet), and a developer who wanted "just the lakehouse" had to
 * reverse-engineer a Playwright config to find out which mocks to start and which env to set.
 *
 * It matters most where there is NO cluster: a cloud sandbox (claude.ai/code, CI) cannot run k3s or
 * build a Dagger image, but it runs this fine — which is exactly how CI already exercises 5 of the 7
 * zones on a stock `ubuntu-latest` runner.
 *
 * WHAT IT DOES NOT DO — read this before assuming a blank page is a bug:
 *
 *   - The mocks are SEED-DRIVEN. They answer 404 to everything until a caller POSTs `/__mock/seed`,
 *     because a mock with baked-in fixtures cannot tell a live surface from a dead one (that is the
 *     mocks' own design note, and it is right). So interactively you get working pages in their EMPTY
 *     state, not populated data. Perfect for layout, nav, empty/error states and the gates; useless
 *     for "does this table paginate 10k rows". Seeding a fixture set per zone is the obvious next
 *     step and is deliberately not smuggled in here.
 *   - AUTH IS OFF. The e2e configs set `OIDC_*` to force the governed path on; this omits them, so
 *     `locals.authEnabled` is false and surfaces render as an anonymous-permitted caller. To develop
 *     the governed path, run the zone's Playwright suite (which mints a sealed cookie) instead.
 *   - CROSS-ZONE LINKS 404. The shared navbar renders all seven zone entries with
 *     `data-sveltekit-reload`; only one zone is listening. That is inherent to the isolation, not a
 *     defect — use `make dev-frontends` and :3024 when you need to cross a zone boundary.
 *
 * DRIFT. Port numbers are NOT restated here: each mock reads its own port from the zone's
 * `e2e/ports.ts`, and this script imports that same module, so the single source stays single. The
 * zone's dev port is likewise read from its `vite.config.ts`, which the frontend skill names as the
 * one place a zone's port is declared. The env MAPPING is the one thing that exists twice (here and
 * in the zone's `playwright.config.ts`) — `@rask/zone-contract`'s `dev-zone.test.ts` fails if the two
 * disagree, so a mock that one loop points at and the other forgets cannot ship.
 */

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/** `frontend/` — three levels up from `packages/zone-contract/src`. Same anchor `manifest.ts` uses. */
const FRONTEND_ROOT = resolve(import.meta.dir, '../../..');

/** A zone's fake-upstream stack: which mock servers to run, and the env that points the zone at them. */
export type ZoneStack = {
	/** Mock server entrypoints, relative to the zone directory. Each is a `Bun.serve` that reads its
	 *  own port from the zone's `e2e/ports.ts`. */
	mocks: string[];
	/** Server-side env for the zone's dev server, built from that zone's own port constants.
	 *  Keys must match what the zone's `playwright.config.ts` sets (minus `OIDC_*`) — gated. */
	env: (port: (name: string) => number) => Record<string, string>;
};

/**
 * The five zones that ship hermetic mocks. `compute` and `studio` are ABSENT ON PURPOSE — they have
 * no `e2e/` directory and no `test:e2e` script, which is the same reason they are the two zones no
 * gate covers. They still start here (the zone runs, its `/api` is simply unmocked) and the script
 * says so out loud rather than pretending otherwise.
 */
export const ZONE_STACKS: Record<string, ZoneStack> = {
	home: {
		mocks: ['e2e/mock-catalog.ts', 'e2e/mock-observability.ts'],
		env: (port) => ({
			CATALOG_API: url(port('MOCK_CATALOG_PORT')),
			GREPTIME_API: url(port('MOCK_OBS_PORT')),
			// The client-side vite proxy target. Left at its default it points at :8001 (the lineage
			// service), which `make dev-micro` does not start — so every browser-side /api/* call fails
			// on a connection refused rather than an honest 404. Pointed at the mock it is at least a
			// real HTTP answer.
			LANCE_BACKEND: url(port('MOCK_CATALOG_PORT')),
		}),
	},
	lakehouse: {
		mocks: [
			'e2e/admin/mock-catalog.ts',
			'e2e/admin/mock-observability.ts',
			'e2e/lineage/mock-lineage.ts',
		],
		env: (port) => ({
			CATALOG_API: url(port('MOCK_CATALOG_PORT')),
			LINEAGE_API: url(port('MOCK_LINEAGE_PORT')),
			GREPTIME_API: url(port('MOCK_OBS_PORT')),
			NATS_MONITOR_API: url(port('MOCK_OBS_PORT')),
			LANCE_BACKEND: url(port('MOCK_CATALOG_PORT')),
		}),
	},
	explorer: {
		mocks: ['e2e/mock-media-services.ts'],
		env: (port) => ({
			CATALOG_API: url(port('MOCK_SERVICES_PORT')),
			SEARCH_API: url(port('MOCK_SERVICES_PORT')),
			ANNOTATOR_API: url(port('MOCK_SERVICES_PORT')),
			ANNOTATOR_PROJECTS_API: url(port('MOCK_SERVICES_PORT')),
		}),
	},
	annotator: {
		mocks: ['e2e/mock-annotator.ts'],
		env: (port) => ({
			ANNOTATOR_API: url(port('MOCK_ANNOTATOR_PORT')),
			ANNOTATOR_PROJECTS_API: url(port('MOCK_ANNOTATOR_PORT')),
			SEARCH_API: url(port('MOCK_ANNOTATOR_PORT')),
		}),
	},
	models: {
		mocks: ['e2e/mock-upstreams.ts'],
		env: (port) => ({
			CATALOG_API: url(port('MOCK_UPSTREAMS_PORT')),
			GREPTIME_API: url(port('MOCK_UPSTREAMS_PORT')),
			LINEAGE_API: url(port('MOCK_UPSTREAMS_PORT')),
			VIEWER_BACKEND: url(port('MOCK_UPSTREAMS_PORT')),
		}),
	},
};

const url = (port: number): string => `http://localhost:${port}`;

/** Every zone directory, so an unknown argument gets a useful list rather than a stack trace. */
function zoneDirs(): string[] {
	const manifest = resolve(FRONTEND_ROOT, 'microfrontends/home/microfrontends.json');
	const raw: unknown = JSON.parse(readFileSync(manifest, 'utf8'));
	const apps =
		typeof raw === 'object' && raw !== null && 'applications' in raw
			? (raw as { applications: Record<string, unknown> }).applications
			: {};
	return Object.keys(apps);
}

/** The zone's dev port, read from `vite.config.ts` — the one place a zone declares it. */
function devPort(zone: string): number | null {
	const config = resolve(FRONTEND_ROOT, `microfrontends/${zone}/vite.config.ts`);
	if (!existsSync(config)) return null;
	const match = /port:\s*(\d+)/.exec(readFileSync(config, 'utf8'));
	return match?.[1] ? Number(match[1]) : null;
}

/** The zone's own port constants, imported (never restated) from its `e2e/ports.ts`. */
async function portsOf(zone: string): Promise<(name: string) => number> {
	const mod: Record<string, unknown> = await import(
		resolve(FRONTEND_ROOT, `microfrontends/${zone}/e2e/ports.ts`)
	);
	return (name: string): number => {
		const value = mod[name];
		if (typeof value !== 'number') {
			throw new Error(
				`${zone}/e2e/ports.ts does not export a numeric ${name} — dev-zone.ts and that file have drifted.`,
			);
		}
		return value;
	};
}

/** Resolve when the port answers HTTP at all. A mock 404 is a healthy mock. */
async function waitForPort(port: number, label: string, tries = 60): Promise<void> {
	for (let i = 0; i < tries; i += 1) {
		try {
			await fetch(`http://localhost:${port}/__mock/ping`);
			return;
		} catch {
			await new Promise((r) => setTimeout(r, 100));
		}
	}
	throw new Error(`${label} never came up on :${port} after ${(tries * 100) / 1000}s`);
}

async function main(): Promise<number> {
	const zone = process.argv[2];
	const known = zoneDirs();

	if (!zone || !known.includes(zone)) {
		console.error(`usage: make dev-zone ZONE=<zone>\n  zones: ${known.join(', ')}`);
		return 2;
	}

	const zoneDir = resolve(FRONTEND_ROOT, `microfrontends/${zone}`);
	const stack = ZONE_STACKS[zone];
	const children: { kill: () => void }[] = [];

	const shutdown = (): void => {
		for (const child of children) {
			try {
				child.kill();
			} catch {
				/* already gone */
			}
		}
	};
	process.on('SIGINT', () => {
		shutdown();
		process.exit(130);
	});
	process.on('SIGTERM', () => {
		shutdown();
		process.exit(143);
	});

	let env: Record<string, string> = {};

	if (!stack) {
		console.error(
			`\n!! ${zone} ships NO hermetic mocks (no e2e/ directory, no test:e2e script), so its /api\n` +
				`   is UNMOCKED — server-side reads will fail on a connection refused. The zone itself runs.\n` +
				`   This is the same gap that leaves ${zone} outside every local gate.\n`,
		);
	} else {
		const port = await portsOf(zone);
		env = stack.env(port);

		for (const mock of stack.mocks) {
			console.error(`==> mock: ${zone}/${mock}`);
			children.push(
				Bun.spawn(['bun', mock], { cwd: zoneDir, stdout: 'inherit', stderr: 'inherit' }),
			);
		}
		// Wait for every distinct mock port before the zone starts: a zone whose first SSR render races
		// an unstarted mock renders its error state and caches it, which reads as "the mock is broken".
		const distinct = [...new Set(Object.values(env))].flatMap((u) => {
			const p = Number(new URL(u).port);
			return Number.isFinite(p) ? [p] : [];
		});
		await Promise.all(distinct.map((p) => waitForPort(p, `${zone} mock`, 60)));
	}

	const port = devPort(zone);

	// Build the shared libs FIRST, then run the zone's OWN vite — deliberately NOT `turbo run dev`.
	//
	// `bun run dev:<zone>` (what `make frontend-<zone>` calls) is `turbo run dev --filter=<zone>...`,
	// and turbo 2.10.7 answers that by ALSO starting its built-in microfrontends proxy on :3024,
	// because the filter's dependency closure reaches `microfrontends.json`. Two consequences, both
	// measured here: a zone "on its own" fails outright with `Microfrontends proxy error: Port is not
	// available` when anything already holds :3024 (a colleague's `make dev-frontends`, another
	// worktree, a stale run), and the `...` closure also starts `@rask/ui`'s `svelte-package -w`
	// watcher — the very writer `make dev-frontends` filters out to stop it rewriting `dist/` under a
	// reading zone. So the documented "one zone" target is not actually isolated.
	//
	// Running the zone's own `vite dev` binds exactly ONE port, needs no proxy, and starts no watcher,
	// which is what lets this coexist with a full composition already running.
	console.error('==> building @rask/ui + @rask/api (turbo, cached)');
	const libs = Bun.spawn(
		[
			'bunx',
			'turbo',
			'run',
			'build',
			'--filter=./packages/ui',
			'--filter=./packages/api',
			'--log-order=grouped',
		],
		{ cwd: FRONTEND_ROOT, stdout: 'inherit', stderr: 'inherit' },
	);
	if ((await libs.exited) !== 0) {
		console.error('!! the shared libs failed to build — the zone would read an incomplete dist/');
		shutdown();
		return 1;
	}

	console.error(
		`\n==> ${zone} on :${port ?? '?'}${port ? ` — browse http://localhost:${port}/${zone === 'home' ? '' : zone}` : ''}` +
			`\n    upstreams: ${Object.keys(env).length ? Object.keys(env).sort().join(', ') : '(none — unmocked)'}` +
			`\n    auth OFF · mocks answer 404 until seeded · cross-zone links 404 (only this zone runs)` +
			`\n    no :3024 proxy and no @rask/ui watcher — safe beside a running \`make dev-frontends\`\n`,
	);

	const dev = Bun.spawn(
		['bun', 'run', 'dev', '--', ...(port ? ['--port', String(port), '--strictPort'] : [])],
		{
			cwd: zoneDir,
			env: { ...process.env, ...env },
			stdout: 'inherit',
			stderr: 'inherit',
		},
	);
	children.push(dev);

	const code = await dev.exited;
	shutdown();
	return code;
}

process.exit(await main());
