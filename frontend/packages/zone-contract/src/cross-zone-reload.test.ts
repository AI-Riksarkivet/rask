import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { findViolations, isCrossZonePath, ZONES } from './cross-zone-reload';

const APPS_ROOT = resolve(import.meta.dirname, '../../../microfrontends');

function zoneDirs(): string[] {
	return readdirSync(APPS_ROOT).filter((d) => statSync(join(APPS_ROOT, d)).isDirectory());
}

function svelteFiles(dir: string): string[] {
	const out: string[] = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const full = join(dir, entry.name);
		if (entry.isDirectory()) {
			if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue;
			out.push(...svelteFiles(full));
		} else if (entry.name.endsWith('.svelte')) {
			out.push(full);
		}
	}
	return out;
}

describe('isCrossZonePath', () => {
	it('matches a link into another zone', () => {
		expect(isCrossZonePath('/default/compute')).toBe(true);
		expect(isCrossZonePath('/default/storage/volumes')).toBe(true);
	});

	it('ignores same-zone links, which are written {base}/…', () => {
		// `{base}/volumes` normalises to `￿/volumes` — the placeholder can never form a zone path.
		expect(isCrossZonePath('￿/volumes')).toBe(false);
	});

	it('ignores the catch-all and non-zone paths', () => {
		expect(isCrossZonePath('/')).toBe(false);
		expect(isCrossZonePath('/api/v1/batches')).toBe(false);
		expect(isCrossZonePath('/default')).toBe(false);
	});

	it('names every zone that owns a base path, and only those', () => {
		// The predicate is a hardcoded list: if a zone is added or renamed and this is not, the
		// gate silently stops protecting it.
		const apps = zoneDirs().filter((z) => z !== 'home');
		expect([...ZONES].sort()).toEqual(apps.sort());
	});
});

describe('findViolations reads the markup', () => {
	it('flags a bare cross-zone link', () => {
		expect(findViolations('<a href="/default/compute">jobs</a>')).toEqual([
			{ href: '/default/compute', line: 1 },
		]);
	});

	it('accepts one that hard-navigates', () => {
		expect(findViolations('<a href="/default/compute" data-sveltekit-reload>jobs</a>')).toEqual([]);
		expect(findViolations('<a data-sveltekit-reload="" href="/default/storage">s</a>')).toEqual([]);
	});

	it('ignores same-zone and fully dynamic hrefs', () => {
		expect(findViolations('<a href="{base}/volumes">v</a>')).toEqual([]);
		expect(findViolations('<a href={hrefVar}>v</a>')).toEqual([]);
		expect(findViolations('<a>no href</a>')).toEqual([]);
	});
});

describe('GATE: no cross-zone link in any zone soft-navigates', () => {
	it('every app is clean', () => {
		const offenders: string[] = [];
		for (const zone of zoneDirs()) {
			const src = join(APPS_ROOT, zone, 'src');
			let files: string[];
			try {
				files = svelteFiles(src);
			} catch {
				continue; // a zone without a src/ yet
			}
			for (const file of files) {
				for (const v of findViolations(readFileSync(file, 'utf8'))) {
					offenders.push(`${relative(APPS_ROOT, file)}:${v.line} → ${v.href}`);
				}
			}
		}
		expect(offenders).toEqual([]);
	});
});
