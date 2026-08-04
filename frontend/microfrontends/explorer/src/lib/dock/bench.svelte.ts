/**
 * The media dock's shared search state — ONE store every panel in the dock reads.
 *
 * This is the whole reason a dock belongs INSIDE its zone: the panels are the zone's real
 * components (`hit-list`, `AtlasMap`, `topic-treemap`, `player-pane`) and they share one live
 * search through ordinary Svelte context, so the results list, the atlas selection and the player
 * can never be a query apart. No bundles, no proxies, no mirroring — the cross-zone workbench had
 * to pay for all three and still could not share a store.
 */
import { createContext } from 'svelte';
import { search, type Hit, type SearchSpec } from '@rask/explorer-api';

export class Bench {
	spec = $state<SearchSpec>({ q: '', n: 100, mode: 'fts' });
	hits = $state<Hit[]>([]);
	active = $state<Hit | null>(null);
	loading = $state(false);
	error = $state<string | null>(null);
	/** Bumped on every settled search — panels that want "did anything change?" read this. */
	revision = $state(0);

	/** Run the CURRENT spec. Latest-wins: a slower earlier query can never overwrite a newer one. */
	#seq = 0;
	async run(next?: Partial<SearchSpec>): Promise<void> {
		if (next) this.spec = { ...this.spec, ...next };
		if (this.spec.q.trim() === '') {
			this.hits = [];
			this.error = null;
			return;
		}
		const mine = ++this.#seq;
		this.loading = true;
		try {
			const rows = await search(this.spec);
			if (mine !== this.#seq) return;
			this.hits = rows;
			this.error = null;
			this.revision += 1;
		} catch (e) {
			if (mine !== this.#seq) return;
			this.hits = [];
			this.error = e instanceof Error ? e.message : String(e);
		} finally {
			if (mine === this.#seq) this.loading = false;
		}
	}

	select(hit: Hit | null): void {
		this.active = hit;
	}
}

export const [getBench, setBench] = createContext<Bench>();
