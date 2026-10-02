/**
 * The web zones' READ-only lineage credential is the pod's projected ServiceAccount token (D1, LH-220),
 * presented as a bearer and read from its file on every request.
 *
 * Read at request time because kubelet rewrites the projected file in place at ~515 s of a 600 s token
 * (measured, P5.3 d): a value taken once when the route module loads is expired ten minutes after the
 * zone starts. Nothing else rides with it: the lineage door takes the subject from the verified token,
 * so a name header or the shared sidecar token would only be a second, unverified claim.
 */
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { RequestHandler } from '@sveltejs/kit';
import { expect, test } from 'vitest';
import { makeLineageProxy, type AuthLocals } from '../src/bff';

type KitEvent = Parameters<RequestHandler>[0];

test('a signed-out lineage read carries the token file read at request time as a bearer, and nothing that names the caller', async () => {
	const tokenFile = join(mkdtempSync(join(tmpdir(), 'lineage-identity-')), 'token');
	writeFileSync(tokenFile, 'jwt-issued-at-start\n');
	const handler = makeLineageProxy({
		LINEAGE_API: 'http://lineage.test',
		LINEAGE_SERVICE_TOKEN_FILE: tokenFile,
		LINEAGE_SERVICE_ID: 'service-web',
	});
	const sent: Record<string, string>[] = [];
	const upstream: typeof fetch = async (_input, init) => {
		sent.push(Object.fromEntries(new Headers(init?.headers)));
		return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
	};
	const locals: AuthLocals = { authEnabled: true, session: null };
	const read = () => {
		const url = new URL('https://zone.test/api/runs');
		return handler({
			url,
			fetch: upstream,
			request: new Request(url),
			locals,
		} as unknown as KitEvent);
	};

	await read();
	writeFileSync(tokenFile, 'jwt-rotated-by-kubelet\n');
	await read();

	expect(sent).toEqual([
		{ authorization: 'Bearer jwt-issued-at-start' },
		{ authorization: 'Bearer jwt-rotated-by-kubelet' },
	]);
});
