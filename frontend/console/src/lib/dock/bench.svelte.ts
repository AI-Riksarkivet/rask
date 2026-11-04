/**
 * The workbench's shared lineage event feed — ONE `EventFeed`, read once, rendered by the Events
 * panel through ordinary context.
 */
import { createContext } from 'svelte';
import { createLineageClient, type EventRecord } from '@rask/api/lineage';
import { getJSON, requestJSON } from '$lib/http';
import { LINEAGE_API } from '$lib/upstreams';

/** How many recent events the panel shows. */
const EVENTS_WINDOW = 200;

/** The OpenLineage event feed, read through the lineage client. */
export class EventFeed {
	events = $state<EventRecord[]>([]);
	/** True once the FIRST read settled (success or failure) — before that the panel says
	 * "connecting", never "no events". */
	settled = $state(false);

	#client = createLineageClient({
		getJSON: (path) => getJSON(LINEAGE_API, path),
		requestJSON: (path, init) => requestJSON(LINEAGE_API, path, init),
	});

	async poll(): Promise<void> {
		try {
			// `getJSON` maps any failure to null: a failed read PRESERVES the last good rows.
			const res = await this.#client.fetchEvents({ limit: EVENTS_WINDOW, summary: true });
			if (res) this.events = res.events ?? [];
		} finally {
			this.settled = true;
		}
	}
}

export function createBench(): EventFeed {
	return new EventFeed();
}

export const [getBench, setBench] = createContext<EventFeed>();
