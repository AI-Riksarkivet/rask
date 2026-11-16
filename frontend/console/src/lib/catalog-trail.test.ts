import { describe, expect, it } from 'vitest';

import { catalogPath, catalogTrail } from './catalog-trail';

describe('catalogPath', () => {
	it('names an object by its path, warehouse first, with no delimiter in sight', () => {
		expect(catalogPath('acme$bronze$e2e_crash_ds')).toBe('acme › bronze › e2e_crash_ds');
	});
});

describe('catalogTrail', () => {
	it('leads a table back through its namespace and its warehouse', () => {
		expect(
			catalogTrail('/catalog/tables/[table]', { table: 'acme$bronze$orders' })?.map((c) => [
				c.label,
				c.href,
				c.page,
			]),
		).toEqual([
			['Catalog', '/catalog', false],
			['Warehouses', '/catalog/warehouses', true],
			['acme', '/catalog/warehouses/acme', true],
			['bronze', '/catalog/namespaces/acme%24bronze', true],
			['orders', '/catalog/tables/acme%24bronze%24orders', true],
		]);
	});

	it('gives each nested namespace a crumb of its own', () => {
		expect(
			catalogTrail('/catalog/namespaces/[id]', { id: 'acme$raw$audio' })?.map((c) => c.href),
		).toEqual([
			'/catalog',
			'/catalog/warehouses',
			'/catalog/warehouses/acme',
			'/catalog/namespaces/acme%24raw',
			'/catalog/namespaces/acme%24raw%24audio',
		]);
	});

	it('leaves every other route to the path', () => {
		expect(catalogTrail('/catalog/tables', {})).toBeNull();
	});
});
