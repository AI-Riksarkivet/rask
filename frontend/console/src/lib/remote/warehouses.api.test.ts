import { afterEach, describe, expect, it, vi } from 'vitest';

import { grantProjectAdmin } from './warehouses.api';

/** A catalog that answers every request 200, recording what it was asked. */
function catalogRecording() {
	const asked: { url: string; method: string; body: unknown }[] = [];
	vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
		asked.push({
			url: String(input),
			method: init?.method ?? 'GET',
			body: JSON.parse(String(init?.body ?? 'null')),
		});
		return new Response(JSON.stringify({ members: [] }), { status: 200 });
	});
	return asked;
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('grantProjectAdmin', () => {
	it("grants the create dialog's extra admin at the project's members door, naming the project by its id", async () => {
		const asked = catalogRecording();

		const answer = await grantProjectAdmin({ project: 'acme', user: 'user:dex~bob' });

		expect(answer.ok).toBe(true);
		expect(asked).toEqual([
			{
				url: '/api/catalog/management/v1/projects/acme/members',
				method: 'PUT',
				body: { user: 'user:dex~bob', relation: 'admin' },
			},
		]);
	});
});
