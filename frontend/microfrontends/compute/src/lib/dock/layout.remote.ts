/**
 * The dock layout, read on the SERVER so it arrives in the SSR payload.
 *
 * Compute is a remote-`query()` zone, so this is its native dialect rather than a graft: the same
 * `query()` + `getRequestEvent().fetch` shape every read in `lib/remote/compute.remote.ts` already
 * uses. Reading here removes one of the two sequential round trips the browser would otherwise pay
 * after hydration, which is what makes the saved layout the FIRST paint instead of a replacement for
 * a flash of seeded defaults.
 *
 * The three-outcome contract survives the trip (`ok` / `absent` / `unreadable`) — an `unreadable` read
 * on the server must still disable saving on the client, or the fast path becomes the one that
 * overwrites a user's workspace.
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
