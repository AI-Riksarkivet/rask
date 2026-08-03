/**
 * The lakehouse elements' SHARED lineage store — a module-level singleton, which is the whole
 * one-poller trick (open_workbench.md): every lakehouse element on a page is instantiated from this
 * ONE loaded script, so module scope is page scope, and graph/runs/events can never be a poll apart
 * — the same property the in-zone dock got from context, recovered across the element boundary.
 *
 * The client chain is root-absolute (`/lakehouse/capi/…` through the ingress, session cookie
 * riding), so it answers regardless of which zone's page mounted the element.
 */
import { createBffClient } from '@rask/api/client';
import { createLineageClient } from '@rask/api/lineage';
import { LineageState } from '../lineage/store.svelte';

/** The one client, shared by the store below AND the elements that read other endpoints
 *  (datasets); root-absolute base, so it answers from any zone's page with the session riding. */
export const client = createLineageClient(createBffClient('/lakehouse'));

export const lineage = new LineageState(client);

let started = false;
/** Called by each element on mount; the FIRST caller starts the one poll loop. Never stopped —
 *  the loop lives exactly as long as the page that loaded this script, like any zone's own store. */
export function ensurePolling(): void {
	if (started) return;
	started = true;
	void lineage.poll();
	// POLL REASON: lineage runs/events move while the workbench is open, and the elements have no
	// push channel across the zone boundary; one interval feeds every lakehouse panel on the page.
	setInterval(() => void lineage.poll(), 15_000);
}
