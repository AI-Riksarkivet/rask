import { beforeEach, describe, expect, it } from 'vitest';
import { apiUrl, resetApiBase, setApiBase } from './base';

beforeEach(() => resetApiBase());

describe('apiUrl carries the zone base', () => {
	it('is root-absolute before a zone sets one', () => {
		expect(apiUrl('/api/health')).toBe('/api/health');
	});

	it('prefixes the zone base', () => {
		setApiBase('/media');
		expect(apiUrl('/api/health')).toBe('/media/api/health');
	});

	it('normalizes a trailing slash rather than doubling it', () => {
		setApiBase('/annotator/');
		expect(apiUrl('/api/jobs')).toBe('/annotator/api/jobs');
	});
});

describe('the base is a per-PROCESS constant, not per-request state', () => {
	// A zone is a process: media and annotator are separate images, pods and dev servers, so this
	// module only ever sees one base. If that ever stopped being true — two zones composed into one
	// SSR process — every media-plane URL would silently point at the other zone's proxy routes.
	it('refuses to re-base to a different zone', () => {
		setApiBase('/media');
		expect(() => setApiBase('/annotator')).toThrow(/already set to "\/media"/);
	});

	it('is idempotent for the same base', () => {
		setApiBase('/media');
		expect(() => setApiBase('/media')).not.toThrow();
		expect(() => setApiBase('/media/')).not.toThrow();
		expect(apiUrl('/api/health')).toBe('/media/api/health');
	});

	it('a later empty base is a no-op, so an unbased consumer cannot clear a zone', () => {
		setApiBase('/media');
		setApiBase('');
		expect(apiUrl('/api/health')).toBe('/media/api/health');
	});
});
