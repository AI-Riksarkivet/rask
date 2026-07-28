/**
 * The dock layout, read on the SERVER so it arrives in the SSR payload. See the lakehouse twin for the
 * full reasoning — in short, it removes one of the two sequential round trips the browser would
 * otherwise pay after hydration, which is what makes the saved layout the FIRST paint rather than a
 * replacement for a flash of seeded defaults.
 *
 * Deliberately per-zone and not shared: a remote function belongs to the app that serves it, and the
 * endpoint is the one genuinely zone-shaped value (each zone reaches the catalog through its own base
 * path and its own BFF). Everything else lives once, in `@rask/api/dock-layout`.
 */
import { getRequestEvent, query } from '$app/server';
import * as v from 'valibot';
import { makeDockLayoutStore, type StoredLayoutRead } from '@rask/api/dock-layout';
import type { SerializedDockview } from 'dockview';
import { base } from '$app/paths';

export const dockLayout = query(
	v.string(),
	async (workbenchId): Promise<StoredLayoutRead<SerializedDockview>> =>
		makeDockLayoutStore<SerializedDockview>({
			workbenchId,
			endpoint: `${base}/capi/v1/user-state/dock-layout`,
			fetcher: getRequestEvent().fetch,
		}).load(),
);
