import { describe, it, expect } from 'vitest';
import { isCrossZonePath } from './cross-zone-reload.js';

// Project-first IA via HOST: cross-zone hrefs are now single-segment, domain-relative
// (`/compute/jobs`, `/overview`), NOT the old `/default/<domain>` two-segment form.
// The guard predicate must match the current scheme or it silently protects nothing.
describe('isCrossZonePath', () => {
	it('matches a nested cross-zone path', () => {
		expect(isCrossZonePath('/compute/jobs')).toBe(true);
		expect(isCrossZonePath('/discover/viewer/vol/1')).toBe(true);
	});

	it('matches a bare domain landing', () => {
		expect(isCrossZonePath('/overview')).toBe(true);
		expect(isCrossZonePath('/storage')).toBe(true);
	});

	it('ignores same-zone / non-zone and opaque-expression hrefs', () => {
		expect(isCrossZonePath('/')).toBe(false);
		expect(isCrossZonePath('/api/serve')).toBe(false);
		// A leading `{base}` expression renders as the opaque placeholder — never a zone.
		expect(isCrossZonePath('￿/jobs')).toBe(false);
	});

	it('does NOT match the retired /default/<domain> two-segment form', () => {
		// Guards against a regression back to the old scheme's regex.
		expect(isCrossZonePath('/default/compute')).toBe(false);
	});
});
