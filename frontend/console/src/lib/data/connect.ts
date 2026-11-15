/**
 * How a pipeline on its own compute reaches a table or namespace: through the catalog's Lance REST
 * namespace (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md`), with the caller's own
 * bearer as the `headers.Authorization` property, and the storage credential the catalog vends on each
 * open. The snippets carry no secret: the bearer is read from the environment, and they name the door
 * (the catalog's URL) and the audience the bearer must carry.
 *
 * Identifiers are passed as lists, the namespace client's own form, so no `$` appears in them.
 */
import { DELIMITER } from '#lib/api/identifiers.ts';
import { catalogPath } from '#lib/catalog-trail.ts';

/** What the console signed in with: its issuer and the scopes the release names. Null on a stack that
 *  signs nobody in. */
export type SignIn = { issuer: string; scopes: readonly string[] } | null;

export type ConnectRequest = {
	/** The catalog's public base, the namespace client's `uri`. */
	catalogUrl: string;
	id: string;
	kind: 'table' | 'namespace';
	signIn: SignIn;
};

/** Dex's scope that puts an audience in the token's `aud`. */
const AUDIENCE_SCOPE = 'audience:server:client_id:';

/** A list of strings as a Python list literal. */
function pyList(items: readonly string[]): string {
	return `[${items.map((item) => JSON.stringify(item)).join(', ')}]`;
}

/** The comment above every snippet: what it reaches, and which bearer it needs. */
function header({ id, kind, signIn }: ConnectRequest): string {
	const reads = kind === 'table' ? 'Reads the table' : 'Lists the tables of the namespace';
	const lines = [
		`# ${reads} ${catalogPath(id)} through the lakehouse's catalog, a Lance REST namespace.`,
	];
	if (signIn === null) {
		lines.push('# This lakehouse signs nobody in, so no bearer is sent.');
	} else {
		const scope = signIn.scopes.find((s) => s.startsWith(AUDIENCE_SCOPE));
		lines.push(
			scope
				? `# RASK_TOKEN is a bearer from ${signIn.issuer} for the audience ${scope.slice(AUDIENCE_SCOPE.length)} (scope "${scope}").`
				: `# RASK_TOKEN is a bearer from ${signIn.issuer} that the lakehouse accepts.`,
		);
	}
	if (kind === 'table') {
		lines.push(
			'# The catalog vends a short-lived storage credential for the table on each open: no storage key is written here.',
		);
	}
	return lines.join('\n');
}

/** The namespace client's properties: the door, and the bearer when the stack signs people in. */
function properties({ catalogUrl, signIn }: ConnectRequest): string {
	const auth =
		signIn === null
			? ''
			: `\n        "headers.Authorization": f"Bearer {os.environ['RASK_TOKEN']}",`;
	return `{\n        "uri": ${JSON.stringify(catalogUrl)},${auth}\n    }`;
}

/** The pylance and LanceDB snippets for one table or namespace. */
export function connectSnippets(request: ConnectRequest): { pylance: string; lancedb: string } {
	const segments = request.id.split(DELIMITER);
	const head = header(request);
	const imports = request.signIn === null ? '' : 'import os\n\n';
	const props = properties(request);
	const name = segments.at(-1) ?? '';
	const parent = pyList(segments.slice(0, -1));

	const pylanceUse =
		request.kind === 'table'
			? `table = lance.dataset(namespace_client=catalog, table_id=${pyList(segments)})\nprint(table.count_rows())\n`
			: `print(catalog.list_tables(ListTablesRequest(id=${pyList(segments)})).tables)\n`;
	const pylanceImports =
		request.kind === 'table'
			? 'import lance\nimport lance_namespace\n'
			: 'import lance_namespace\nfrom lance_namespace import ListTablesRequest\n';
	const pylance = `${head}\n${imports}${pylanceImports}\ncatalog = lance_namespace.connect(\n    "rest",\n    ${props},\n)\n${pylanceUse}`;

	const lancedbUse =
		request.kind === 'table'
			? `table = db.open_table(${JSON.stringify(name)}, namespace_path=${parent})\nprint(table.count_rows())\n`
			: `print(db.list_tables(namespace_path=${pyList(segments)}).tables)\n`;
	const lancedb = `${head}\n${imports}import lancedb\n\ndb = lancedb.connect_namespace(\n    "rest",\n    ${props},\n)\n${lancedbUse}`;

	return { pylance, lancedb };
}
