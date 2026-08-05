/**
 * The flows engine status — a remote `query()` against the `flows` service's
 * catalog through the gateway (`/api/flows/catalog`). v0 renders it as a chip
 * only: client-side runs work without the service, server-side (durable) runs
 * are what it exists for. See open_studio_flows.md.
 */
import { getRequestEvent, query } from '$app/server';
import * as v from 'valibot';
import { EngineCatalogSchema, type EngineStatus } from '../serve-contract';

export const getFlowsEngine = query(async (): Promise<EngineStatus> => {
	try {
		const res = await getRequestEvent().fetch('/api/flows/catalog');
		if (!res.ok) return { ok: false };
		const parsed = v.safeParse(EngineCatalogSchema, await res.json());
		return parsed.success ? { ok: true, kinds: parsed.output.kinds.length } : { ok: false };
	} catch {
		return { ok: false };
	}
});
