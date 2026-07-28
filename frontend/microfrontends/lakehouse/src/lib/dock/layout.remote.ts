/**
 * The dock layout, read on the SERVER so it arrives in the SSR payload.
 *
 * Without this the browser pays two SEQUENTIAL round trips after hydration — download the dock chunk,
 * then read the layout — and the seeded default layout paints before the saved one replaces it. That
 * replacement IS the blink. Reading it here means the first frame the dock draws is already the user's.
 *
 * The three-outcome contract survives the trip intact (`ok` / `absent` / `unreadable`), which is the
 * reason this returns a `StoredLayoutRead` rather than a bare layout: an `unreadable` read on the
 * server must still disable saving on the client, or the fast path becomes the one that overwrites a
 * user's workspace.
 *
 * `getRequestEvent().fetch` resolves the zone-relative URL against this app and carries the session
 * cookie, so the `capi` proxy attaches the same bearer it would for a browser call — and SvelteKit
 * serves a same-app route internally rather than making a real network hop.
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
