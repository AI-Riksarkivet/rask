<script lang="ts">
	// "More like this" — build a labelling batch from ONE example.
	//
	// `open_browse.md` §3, and the reason it is the highest value-per-unit-work selector: the bulk bar
	// could already label a selection, but a selection had to be assembled by hand, one document at a
	// time. The way anyone actually builds a labelling batch is "this page — find four hundred more
	// like it", and the embeddings to answer that were already sitting in the table.
	//
	// Nothing new server-side beyond the endpoint: this reads neighbours and hands their keys to the
	// SAME selection the bulk bar already sends.
	import { Button } from '@rask/ui/button';
	import { Sparkles } from '@lucide/svelte';

	import { findSimilar } from './remote/similar.remote';
	import { describeNeighbour, keyOfHit, newlyAdded, type SimilarHit } from './similar';

	let {
		seedKey,
		keyFields,
		dataset = null,
		table = null,
		selected = [],
		onadd,
	}: {
		/** The row to find neighbours of — identity fields joined by `/`. */
		seedKey: string;
		/** The corpus's identity fields, in order. Reading them from the descriptor rather than
		 *  guessing is what keeps a neighbour's key identical to every other key in the zone. */
		keyFields: string[];
		dataset?: string | null;
		table?: string | null;
		/** Already-selected keys — so the panel can report how many are actually NEW. */
		selected?: string[];
		/** Hands the neighbours' keys to whoever owns the selection. */
		onadd?: (keys: string[]) => void;
	} = $props();

	let open = $state(false);
	let added = $state<number | null>(null);

	/** Loaded only once someone asks. A browse page nobody is building a batch on should not fan out
	 *  a vector search per chunk it renders. */
	const neighbours = $derived(
		open && seedKey ? findSimilar({ key: seedKey, dataset, table, n: 24 }) : null,
	);

	const hits = $derived((neighbours?.current?.ok ? neighbours.current.data : []) as SimilarHit[]);

	/** The neighbour keys, minus the seed — the service already drops it, but this panel is also the
	 *  place a stale result would show up, and adding the thing you clicked to your own batch is the
	 *  one outcome that would look like it worked. */
	const keys = $derived(hits.map((h) => keyOfHit(h, keyFields)).filter((k) => k && k !== seedKey));

	function addAll(): void {
		added = newlyAdded(selected, keys);
		onadd?.(keys);
	}
</script>

<div class="flex flex-col gap-2">
	<Button
		size="sm"
		variant="ghost"
		data-testid="similar-open"
		title="find items whose embedding is nearest to this one"
		onclick={() => {
	open = !open;
	added = null;
}}
	>
		<Sparkles class="size-4" />
		{open ? 'Hide similar' : 'More like this'}
	</Button>

	{#if open}
		{#if neighbours?.loading}
			<p class="text-muted-foreground text-xs">Searching the embedding space…</p>
		{:else if neighbours?.current && !neighbours.current.ok}
			<!-- The SERVICE's own words. Its refusals are the actionable half — "this corpus declares no
			     vector space", "that row has no embedding yet", "key has 2 parts, expected 3" — and
			     collapsing them into "search failed" throws away the only thing that says what to do. -->
			<p class="text-destructive text-xs" data-testid="similar-error">
				{neighbours.current.status === 0
					? `The search service is unreachable — ${neighbours.current.detail}`
					: neighbours.current.detail}
			</p>
		{:else if keys.length === 0}
			<p class="text-muted-foreground text-xs" data-testid="similar-empty">
				Nothing else in this corpus is near it.
			</p>
		{:else}
			<ul class="max-h-64 overflow-y-auto text-xs" data-testid="similar-list">
				{#each hits as hit, i (keyOfHit(hit, keyFields))}
					{@const key = keyOfHit(hit, keyFields)}
					{#if key && key !== seedKey}
						<li class="border-border flex items-center justify-between gap-2 border-b py-1">
							<span class="truncate font-mono">{key}</span>
							<!-- WHY this candidate is here. The design's load-bearing requirement for this
							     pane: a bulk surface that cannot explain its selection is one nobody trusts
							     enough to accept in bulk, which is the only thing it exists for. -->
							<span class="text-muted-foreground shrink-0">
								{describeNeighbour(i, typeof hit._distance === 'number' ? hit._distance : undefined)}
							</span>
						</li>
					{/if}
				{/each}
			</ul>

			<div class="flex items-center gap-2">
				<Button size="sm" data-testid="similar-add" onclick={addAll}>
					Add {keys.length} to selection
				</Button>
				{#if added !== null}
					<!-- How many were actually NEW. "Added 24" when 20 were already selected is a lie the
					     user acts on — they size the next step by it. Overlap is not hypothetical here:
					     neighbours-of-A and neighbours-of-B overlap by construction when A and B are
					     similar, which is exactly when someone runs both. -->
					<span class="text-muted-foreground text-xs" data-testid="similar-added">
						{added} new
					</span>
				{/if}
			</div>
		{/if}
	{/if}
</div>
