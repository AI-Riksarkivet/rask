import { describe, expect, it } from 'vitest';

import { connectSnippets } from './connect';

const SIGN_IN = {
	issuer: 'http://localhost:8080/dex',
	scopes: ['openid', 'email', 'audience:server:client_id:rask-lakehouse'],
};

describe('connectSnippets', () => {
	it('reads a table with pylance through the catalog’s namespace door, on a bearer from the env', () => {
		const { pylance } = connectSnippets({
			catalogUrl: 'http://localhost:8080/api/catalog',
			id: 'acme$bronze$orders',
			kind: 'table',
			signIn: SIGN_IN,
		});
		expect(pylance)
			.toBe(`# Reads the table acme › bronze › orders through the lakehouse's catalog, a Lance REST namespace.
# RASK_TOKEN is a bearer from http://localhost:8080/dex for the audience rask-lakehouse (scope "audience:server:client_id:rask-lakehouse").
# The catalog vends a short-lived storage credential for the table on each open: no storage key is written here.
import os

import lance
import lance_namespace

catalog = lance_namespace.connect(
    "rest",
    {
        "uri": "http://localhost:8080/api/catalog",
        "headers.Authorization": f"Bearer {os.environ['RASK_TOKEN']}",
    },
)
table = lance.dataset(namespace_client=catalog, table_id=["acme", "bronze", "orders"])
print(table.count_rows())
`);
	});

	it('lists a namespace’s tables with LanceDB', () => {
		const { lancedb } = connectSnippets({
			catalogUrl: 'https://lake.example/api/catalog',
			id: 'acme$bronze',
			kind: 'namespace',
			signIn: SIGN_IN,
		});
		expect(lancedb).toContain('db = lancedb.connect_namespace(\n    "rest",');
		expect(lancedb).toContain('print(db.list_tables(namespace_path=["acme", "bronze"]).tables)');
	});

	it('names no audience where the release names none', () => {
		const { pylance } = connectSnippets({
			catalogUrl: 'https://lake.example/api/catalog',
			id: 'acme$bronze$orders',
			kind: 'table',
			signIn: { issuer: 'https://login.example', scopes: ['openid'] },
		});
		expect(pylance).toContain(
			'# RASK_TOKEN is a bearer from https://login.example that the lakehouse accepts.\n',
		);
	});
});
