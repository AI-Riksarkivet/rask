import { describe, expect, it } from 'vitest';
import { resolve, routes } from './proxy';

// The proxy is the local stand-in for the cluster Ingress, so its routing table has to match the same
// microfrontends.json the zone-contract tests check — and it has to route the SAME way nginx does
// (longest prefix first), or a zone whose prefix is a prefix of another silently steals its traffic.
describe('zone proxy routing', () => {
	const table = routes();

	it('has a route per zone, and one catch-all', () => {
		expect(table.length).toBeGreaterThan(1);
		expect(table.filter((r) => r.prefix === '')).toHaveLength(1);
	});

	it('orders longest prefix first, like the Ingress', () => {
		const lengths = table.map((r) => r.prefix.length);
		expect([...lengths].sort((a, b) => b - a)).toEqual(lengths);
		// …so the catch-all can never shadow a zone.
		expect(table.at(-1)?.prefix).toBe('');
	});

	it.each([
		['/lakehouse', 'lakehouse'],
		['/lakehouse/data/tables', 'lakehouse'],
		['/media', 'media'],
		['/media/atlas', 'media'],
		['/annotator/', 'annotator'],
		['/', 'home'],
		['/auth/login', 'home'],
	])('%s -> %s', (path, zone) => {
		expect(resolve(table, path)?.zone).toBe(zone);
	});

	it('does not let a zone claim a path that merely starts with its name', () => {
		// `/mediaX` is not the media zone; only an exact segment boundary counts.
		expect(resolve(table, '/mediaXYZ')?.zone).toBe('home');
	});
});
