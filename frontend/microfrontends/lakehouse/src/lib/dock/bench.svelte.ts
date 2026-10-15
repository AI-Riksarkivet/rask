/**
 * The lakehouse dock's shared lineage event feed — ONE `EventFeed`, read once, rendered by the Events
 * panel through ordinary context.
 */
import { createContext } from 'svelte';
import { createLineageClient, type EventRecord } from '@rask/api/lineage';
import { bff } from '$lib/http';

/** How many recent events the panel shows. */
const EVENTS_WINDOW = 200;

/** The OpenLineage event feed, read through the zone's own client. */
export class EventFeed {
	events = $state<EventRecord[]>([]);
	/** True once the FIRST read settled (success or failure) — before that the panel says
	 * "connecting", never "no events". */
	settled = $state(false);

	#client = createLineageClient(bff);

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
