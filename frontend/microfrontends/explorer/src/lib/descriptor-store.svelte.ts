/**
 * App-level descriptor store: loads the active dataset's {@link DatasetView}
 * once at startup and publishes it (both reactively and via the module-level
 * active-view singleton the pure helpers read).
 *
 * Which dataset: a `?dataset=<id>` query param selects a non-default dataset
 * (the acid test serves the same build at `/` and `/?dataset=smoke`); with no
 * param the backend's default DB is used, its id derived from the health
 * endpoint's db path.
 */

import { getDatasetView } from '@rask/explorer-api';
import { setActiveTable, setActiveView, type DatasetView } from '@rask/explorer-api/descriptor';
import { serviceHealth } from '$lib/service-health.svelte';

class DescriptorStore {
	view = $state<DatasetView | null>(null);
	error = $state<string | null>(null);
	/** The backend's DEFAULT corpus id, derived from the health endpoint's db path.
	 *
	 *  Published because the picker needs it and cannot re-derive it: the default corpus is reached
	 *  by having NO `?dataset=` at all, so an entry that does not know it is the default would link
	 *  to `?dataset=<its own id>` — which works, but makes the plain URL and the explicit one two
	 *  different strings for one place. Set even when `?dataset=` selected something else, so the
	 *  picker can always offer the way back. */
	defaultId = $state<string | null>(null);

	/** The dataset id from `?dataset=`, or null for the default DB. */
	private paramId(): string | null {
		if (typeof location === 'undefined') return null;
		return new URLSearchParams(location.search).get('dataset');
	}

	async load(): Promise<void> {
		try {
			const param = this.paramId();
			let id: string;
			let isDefault: boolean;
			if (param) {
				id = param;
				isDefault = false;
			} else {
				// Through the shared store, not a direct `getHealth()`: this is the zone's ONLY health
				// fetcher, so the sidebar badge, the search-bar mode selector and this derivation all
				// read one response. Fetching it here independently made a third request for the same
				// endpoint on every page load, and let the badge and the modes disagree about whether
				// search works — the exact drift `service-health.svelte.ts` documents as the reason it
				// is a single store.
				const health = await serviceHealth.ensure();
				if (health === null) throw new Error(serviceHealth.error ?? 'health unavailable');
				// `db` is null when the service is up but no corpus dataset resolves (an empty corpus
				// volume — the chart default). That is a DIFFERENT failure from an unreachable backend and
				// must read as one: `db_error` is the backend's own reason, so the page says "no dataset"
				// rather than blaming the network, and nothing throws on a property of null.
				if (!health.db) {
					throw new Error(health.db_error ?? 'no dataset is loaded on the media service');
				}
				id =
					health.db.path
						.replace(/\.lance$/, '')
						.split('/')
						.pop() ?? '';
				isDefault = true;
			}
			if (isDefault) this.defaultId = id;
			// The TABLE selection, published the same way the view is. `explorer-api` cannot read
			// `location` (it renders under SSR), so the app is what tells it — and doing it here, beside
			// the dataset it belongs to, is what stops the two halves of one selection drifting apart.
			setActiveTable(
				typeof location === 'undefined' ? null : new URLSearchParams(location.search).get('table'),
			);
			const view = await getDatasetView(id, isDefault);
			setActiveView(view);
			this.view = view;
			// Test hook: expose the active dataset id + identity so an e2e check can
			// prove which dataset the same build is currently rendering.
			if (typeof window !== 'undefined') {
				(window as unknown as { __activeDataset?: unknown }).__activeDataset = {
					id: view.id,
					keyFields: view.keyFields,
					hasTime: view.hasTime,
				};
			}
		} catch (e) {
			this.error = e instanceof Error ? e.message : String(e);
		}
	}
}

export const descriptor = new DescriptorStore();
