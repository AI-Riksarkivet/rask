/**
 * The workbench's shared lineage event feed — ONE `EventFeed` for the page, held in this module.
 *
 * A module and not the panel, because the dock renders only the active tab of a group and remounts
 * a panel when it is moved, maximized or popped out: anything a panel must show again after that
 * lives here. Not in context either: a popped-out panel is mounted in another window, outside the
 * page's component tree, and reads this same instance.
 */
import { createLineageClient, type EventRecord } from '@rask/api/lineage';
import { getJSON, requestJSON } from '#lib/http.ts';
import { LINEAGE_API } from '#lib/upstreams.ts';

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

export const eventFeed = new EventFeed();
