import { describe, it, expect } from 'vitest';
import { collapseCrumbs, pathCrumbs, projectFromHost } from '../src/lib/shell/breadcrumb.js';

// Project-first IA via HOST: the project is the request host, so the pathname
// carries only the domain + in-domain trail. Every path segment is a crumb (the
// DOMAIN is the first crumb, never the project); the project is the breadcrumb
// root and comes from the host, not the path.
describe('pathCrumbs', () => {
	it('keeps the domain as the first crumb on a nested path', () => {
		// /catalog/tables -> Catalog > Tables (the domain must NOT be dropped)
		expect(pathCrumbs('/catalog/tables')).toEqual([
			{ id: 'catalog', label: 'Catalog', href: '/catalog' },
			{ id: 'catalog/tables', label: 'Tables', href: '/catalog/tables' },
		]);
	});

	it('yields a single domain crumb for a bare domain landing', () => {
		expect(pathCrumbs('/workbench')).toEqual([
			{ id: 'workbench', label: 'Workbench', href: '/workbench' },
		]);
	});

	it('keeps repeated segments unique', () => {
		// A namespace named `namespaces` repeats its route's segment.
		expect(pathCrumbs('/catalog/namespaces/namespaces').map((c) => c.id)).toEqual([
			'catalog',
			'catalog/namespaces',
			'catalog/namespaces/namespaces',
		]);
	});

	it('uppercases an initialism segment instead of sentence-casing it', () => {
		// `/admin/dlq` would read "Dlq", a misspelling. The nav calls that page DLQ — a breadcrumb that
		// renames its destination is worse than an ugly one.
		expect(pathCrumbs('/admin/dlq').at(-1)?.label).toBe('DLQ');
	});

	it('leaves ordinary words alone — the initialism list is CLOSED, not a heuristic', () => {
		// The tempting rule ("short segment ⇒ acronym") would shout at every one of these.
		expect(pathCrumbs('/admin/events').at(-1)?.label).toBe('Events');
		expect(pathCrumbs('/settings/audit').at(-1)?.label).toBe('Audit');
	});

	it('percent-decodes a segment for the label but keeps the href encoded', () => {
		// A table id reaches the URL encoded (silver$features -> silver%24features); the crumb must READ
		// as the id, while still linking to the path that resolves.
		const crumbs = pathCrumbs('/catalog/tables/silver%24features');
		expect(crumbs.at(-1)).toEqual({
			id: 'catalog/tables/silver%24features',
			label: 'silver$features',
			href: '/catalog/tables/silver%24features',
		});
		// A segment the route declares a parameter is a catalog name, printed as written even when it
		// looks like route words: no un-dashing, no casing.
		expect(
			pathCrumbs('/catalog/warehouses/acme-wh', '/catalog/warehouses/[id]').map((c) => c.label),
		).toEqual(['Catalog', 'Warehouses', 'acme-wh']);
		// A route group names no path segment, so it does not shift the pairing.
		expect(
			pathCrumbs('/catalog/warehouses/acme-wh', '/(lakehouse)/catalog/warehouses/[id]').map(
				(c) => c.label,
			),
		).toEqual(['Catalog', 'Warehouses', 'acme-wh']);
	});

	it('falls back to the raw segment when the escape is malformed', () => {
		// decodeURIComponent throws on a lone '%': the trail must still render.
		expect(pathCrumbs('/catalog/tables/100%').at(-1)?.label).toBe('100%');
	});

	it('returns no crumbs for the root path', () => {
		expect(pathCrumbs('/')).toEqual([]);
		expect(pathCrumbs('')).toEqual([]);
	});
});

describe('projectFromHost', () => {
	it('reads the project label from a real subdomain host', () => {
		expect(projectFromHost('demo.localhost')).toBe('demo');
		expect(projectFromHost('demo.rask.example.com')).toBe('demo');
	});

	it('has no project on a bare host', () => {
		expect(projectFromHost('localhost')).toBeNull();
	});

	it('has no project on an IPv4 host (first label is an octet, not a project)', () => {
		expect(projectFromHost('127.0.0.1')).toBeNull();
	});
});

// A deep path does not fit a narrow breadcrumb row, so the MIDDLE of the trail folds behind an
// ellipsis menu. The invariant that matters: the current page — the last crumb — is never what
// disappears, and nothing is lost, only moved off the row.
describe('collapseCrumbs', () => {
	const trail = pathCrumbs('/catalog/tables/gold%24catalog');

	it('leaves a trail that already fits untouched', () => {
		expect(collapseCrumbs(trail, 4)).toEqual({ lead: [], hidden: [], tail: trail });
	});

	it('folds the middle, keeping the first crumb and the current page', () => {
		const { lead, hidden, tail } = collapseCrumbs(trail, 2);
		expect(lead.map((c) => c.label)).toEqual(['Catalog']);
		expect(hidden.map((c) => c.label)).toEqual(['Tables']);
		expect(tail.map((c) => c.label)).toEqual(['gold$catalog']);
		// Nothing is dropped — every crumb is still somewhere, so every ancestor stays reachable.
		expect([...lead, ...hidden, ...tail]).toEqual(trail);
	});

	it('clamps maxVisible to a lead plus the current page', () => {
		expect(collapseCrumbs(trail, 0)).toEqual(collapseCrumbs(trail, 2));
		expect(collapseCrumbs(trail, 1)).toEqual(collapseCrumbs(trail, 2));
	});
});
