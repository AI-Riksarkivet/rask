import { describe, expect, it } from 'vitest';
import { exact, norm, seg, under } from './nav-match.ts';

// The matchers the sidebar's rows are built with.
describe('PageNav matchers', () => {
	it('seg: matches the exact route and anything nested under it', () => {
		const m = seg('/catalog/tables');
		expect(m('/catalog/tables')).toBe(true);
		expect(m('/catalog/tables/x')).toBe(true);
		expect(m('/catalog/namespaces')).toBe(false);
	});

	it('exact: matches only its own path — the root-leaf case', () => {
		// Home (`/`) is a prefix of every route; `seg` would keep it lit on every one.
		const m = exact('/');
		expect(m('/')).toBe(true);
		expect(m('/catalog/tables')).toBe(false);
	});

	it('norm + matchers tolerate a trailing slash', () => {
		expect(norm('/catalog/')).toBe('/catalog');
		expect(norm('/')).toBe('/');
		expect(exact('/catalog')('/catalog/')).toBe(true);
		expect(under('/catalog')('/catalog/')).toBe(true);
	});
});
