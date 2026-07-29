<script lang="ts">
	/** Governed-search box (P1 Search tier 1): debounced query → hits with WHY-it-matched chips.
	 * Self-contained: the caller supplies the fetcher (`search`) and the select handler, so the lib
	 * stays transport-agnostic (rask convention: components never own API clients).
	 * The debounce reacts to the BOUND state via $effect rather than a DOM handler — Svelte 5
	 * delegates common DOM events, and a workspace-lib component must not depend on sharing the
	 * host app's delegation root (a handler that silently never fires is worse than no handler). */
	import { Chip } from '../chip';

	export type SearchHit = {
		name: string;
		namespace?: string | null;
		tags?: string[];
		matches?: string[];
	};

	let {
		search,
		onselect,
		placeholder = 'Search datasets, tags, columns…',
		debounceMs = 250,
	}: {
		search: (q: string) => Promise<SearchHit[]>;
		onselect: (name: string) => void;
		placeholder?: string;
		debounceMs?: number;
	} = $props();

	let q = $state('');
	let hits = $state<SearchHit[]>([]);
	let open = $state(false);

	/** Monotonic request id — the LATEST-WINS guard (audit B3). `clearTimeout` only cancels a debounce
	 * that has not fired yet; once the fetch is in flight nothing stopped its stale response from
	 * overwriting a newer one (the old comment claimed otherwise). Mirrors the store's `#colReq`/`#fieldReq`,
	 * which got this right. Bumping on every input AND on pick/clear also kills the second bug: an
	 * in-flight resolve used to set `open = true` unconditionally, reopening the dropdown under a box the
	 * user had already cleared or picked from. */
	let req = 0;

	$effect(() => {
		const value = q.trim();
		const mine = ++req;
		if (!value) {
			hits = [];
			open = false;
			return;
		}
		const timer = setTimeout(async () => {
			let next: SearchHit[];
			try {
				next = await search(value);
			} catch {
				next = []; // a failed fetch degrades to "no matches", never a wedged-closed dropdown
			}
			if (mine !== req) return; // a newer keystroke (or a pick/clear) superseded this one — drop it
			hits = next;
			open = true;
		}, debounceMs);
		return () => clearTimeout(timer);
	});

	function pick(name: string) {
		req++; // supersede any in-flight search so it cannot reopen the dropdown after selection
		open = false;
		q = '';
		hits = [];
		onselect(name);
	}
</script>

<div class="searchbar">
	<input type="search" bind:value={q} {placeholder} aria-label="search" />
	{#if open}
		<ul class="results" role="listbox">
			{#if hits.length === 0}
				<li class="empty">no matches</li>
			{/if}
			{#each hits as hit (hit.name)}
				<li>
					<button type="button" onclick={() => pick(hit.name)}>
						<span class="name">{hit.name}</span>
						<span class="chips">
							{#each hit.matches ?? [] as reason (reason)}
								<Chip label={reason} tone={reason === 'name' ? 'accent' : 'neutral'} />
							{/each}
						</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.searchbar {
		position: relative;
		min-width: 16rem;
	}
	input {
		width: 100%;
		padding: 0.4rem 0.7rem;
		border-radius: 0.5rem;
		border: 1px solid color-mix(in oklab, currentColor 25%, transparent);
		background: transparent;
		color: inherit;
		font: inherit;
	}
	.results {
		position: absolute;
		z-index: 30;
		inset-inline: 0;
		top: calc(100% + 0.25rem);
		margin: 0;
		padding: 0.25rem;
		list-style: none;
		border-radius: 0.5rem;
		border: 1px solid color-mix(in oklab, currentColor 20%, transparent);
		background: var(--panel, Canvas);
		max-height: 18rem;
		overflow: auto;
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.25);
	}
	.results button {
		display: flex;
		width: 100%;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.4rem 0.55rem;
		border: 0;
		background: transparent;
		color: inherit;
		font: inherit;
		border-radius: 0.4rem;
		cursor: pointer;
		text-align: left;
	}
	.results button:hover {
		background: color-mix(in oklab, currentColor 10%, transparent);
	}
	.name {
		font-family: var(--mono, monospace);
		font-size: 0.85rem;
	}
	.chips {
		display: flex;
		gap: 0.25rem;
		flex-wrap: wrap;
	}
	.empty {
		padding: 0.4rem 0.55rem;
		opacity: 0.6;
		font-size: 0.85rem;
	}
</style>
