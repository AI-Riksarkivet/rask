import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
	chartApps,
	devScript,
	FRONTEND_ROOT,
	packageName,
	routingConfig,
	svelteBase,
	vitePort,
	zoneDirs,
} from './manifest';
import { e2eServers } from './workspace';

// THE ZONE MANIFEST: which zones exist, which path each owns, which port each binds. Every other file
// in this package checks something downstream of these facts.

const zones = zoneDirs();
const config = routingConfig();
const apps = chartApps();

/** The catch-all zone: owns '/', so no base path and no routing entry of its own. */
const CATCH_ALL = 'home';

describe('every zone directory is a declared application', () => {
	it('has a zone to test', () => {
		expect(zones.length).toBeGreaterThan(1);
	});

	it.each(zones)('%s is in microfrontends.json', (zone) => {
		// media and annotator were real, Ingress-routed zones that this file did not mention at all, so
		// the dev composition proxy had no route to them.
		expect(Object.keys(config.applications)).toContain(zone);
	});

	it.each(zones)('%s is in the chart frontend.apps list', (zone) => {
		expect(apps.map((a) => a.name)).toContain(zone);
	});

	it('declares no application that has no zone directory', () => {
		expect(Object.keys(config.applications).sort()).toEqual(zones);
	});

	it('lists no chart app that has no zone directory', () => {
		expect(apps.map((a) => a.name).sort()).toEqual(zones);
	});

	it.each(zones)('%s is the package name too, so turbo can route to it', (zone) => {
		// Turborepo resolves microfrontends.json keys against workspace PACKAGE names. media and
		// annotator kept the standalone repo's @lance-media/* scope, so turbo could not match them to
		// their routing entries and the local proxy silently skipped both zones.
		expect(packageName(zone)).toBe(zone);
	});
});

describe('base path, routing config and Ingress route agree', () => {
	it.each(zones.filter((z) => z !== CATCH_ALL))('%s owns /<zone> everywhere', (zone) => {
		// The base path IS the asset prefix (micro-frontends: independently deployed apps serve assets
		// from one origin, so each needs a unique prefix). If it drifts from the path the proxy and the
		// Ingress route, the zone's own chunks 404.
		expect(svelteBase(zone)).toBe(`/${zone}`);
		expect(config.applications[zone]?.routing?.[0]?.paths).toEqual([`/${zone}`, `/${zone}/:path*`]);
		expect(apps.find((a) => a.name === zone)?.path).toBe(`/${zone}`);
	});

	it('the catch-all zone has no base path and owns "/" in the chart', () => {
		expect(svelteBase(CATCH_ALL)).toBe('');
		expect(config.applications[CATCH_ALL]?.routing).toBeUndefined();
		expect(apps.find((a) => a.name === CATCH_ALL)?.catchAll).toBe(true);
	});
});

describe('dev ports are unique, strict, and single-sourced', () => {
	it('no two zones claim the same port', () => {
		// annotator and models both claimed 5176; models bound it with strictPort and won the race, so
		// the annotator drifted to another port and every /annotator link landed on models.
		const ports = zones.map((z) => config.applications[z]?.development?.local?.port);
		expect(new Set(ports).size).toBe(ports.length);
		expect(ports.every((p) => typeof p === 'number')).toBe(true);
	});

	it.each(zones)('%s binds the port the routing config declares, strictly', (zone) => {
		const declared = config.applications[zone]?.development?.local?.port;
		expect(vitePort(zone)).toEqual({ port: declared, strict: true });
	});

	it.each(zones)('%s does not ALSO set a port in its dev script', (zone) => {
		// Two places to change a port is how they drift; vite.config.ts is the single source.
		expect(devScript(zone)).not.toMatch(/--port/);
	});
});

describe('no two zones bind the same port, in dev OR in e2e', () => {
	// The dev ports are checked above against microfrontends.json. The E2E ports are not in any
	// manifest, and playwright runs with `reuseExistingServer` locally — so a duplicate does not fail
	// loudly with EADDRINUSE, it silently ADOPTS whatever is already listening. The lakehouse admin
	// suite's mock catalog sat on 5297, which is the media zone's e2e server; a parallel local run
	// pointed the admin tests at a real dev server and called it the mock.
	const declared = zones.flatMap((zone) =>
		['vite.config.ts', 'playwright.config.ts', ...e2eServers(zone)]
			.map((f) => resolve(FRONTEND_ROOT, `microfrontends/${zone}`, f))
			.filter((p) => existsSync(p))
			.flatMap((p) => {
				const src = readFileSync(p, 'utf8');
				// Only a port a zone BINDS: `port: 5294` (vite `server.port`, playwright `webServer.port`)
				// and `const MOCK_CATALOG_PORT = 5292`. Deliberately not any 4-digit literal — a proxy
				// TARGET (`http://127.0.0.1:5177`, `http://localhost:8001`) is a zone pointing at something
				// else, and two zones proxying the same backend is correct, not a collision.
				const found = [...src.matchAll(/(?:\bport:\s*|PORT\s*=\s*)(\d{4})/g)];
				return found.map((m) => ({ zone, port: Number(m[1]) }));
			}),
	);

	it('finds the ports to compare', () => {
		expect(declared.length).toBeGreaterThan(zones.length);
	});

	it.each([...new Set(declared.map((d) => d.port))])(
		'%d is claimed by exactly one zone',
		(port) => {
			const claimants = [...new Set(declared.filter((d) => d.port === port).map((d) => d.zone))];
			expect(claimants).toHaveLength(1);
		},
	);
});
