/**
 * Value shapes for the flows remote functions — kept OUT of the `.remote.ts`
 * modules because a remote module may export only remote functions (the same
 * sibling-contract convention as the lakehouse's FGA workbench).
 */
import * as v from 'valibot';

/** One live Ray Serve app, reduced to what the Model node's picker needs. */
export interface ServeAppInfo {
	name: string;
	/** Route prefix WITHOUT the leading slash — the `?app=` value for `/api/infer`. */
	app: string;
	status: string;
}

/** Serve discovery through the gateway's read-only `/api/serve` row. Status
 *  union, not exceptions — an unreachable Serve is a rendered state ("discovery
 *  offline, type the app name"), not a crash. */
export type ServeAppsResult = { ok: true; apps: ServeAppInfo[] } | { ok: false; detail: string };

/** The flows engine's catalog shape — pinned by `services/flows` (v0 reads only
 *  enough to render the "server engine" status chip; the palette is client-side). */
export const EngineCatalogSchema = v.object({
	version: v.number(),
	kinds: v.array(v.object({ kind: v.string(), label: v.string() })),
});

export type EngineStatus = { ok: true; kinds: number } | { ok: false };
