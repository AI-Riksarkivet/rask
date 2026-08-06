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

	import { Input } from '@rask/ui/input';
	import { MAX_SIMILAR_N } from './similar-limits';
	import { findSimilar } from './remote/similar.remote';
	import {
		DEFAULT_MAX_DISTANCE,
		describeCutoff,
		describeNeighbour,
		distanceBounds,
		keyOfHit,
		newlyAdded,
		withinDistance,
		type SimilarHit,
	} from './similar';

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

	/** KNOB 1 — BREADTH. How many neighbours to ask for. Was hardcoded at 24, which quietly decided
	 *  the size of every propagated batch in the estate. */
	let howMany = $state(24);

	/** KNOB 2 — THE CUTOFF. The maximum embedding distance a neighbour may have and still be
	 *  propagated to. This is the one that matters: `n` alone says "give me forty" whether or not
	 *  forty are actually alike, so without it the fortieth gets the label because it was RETURNED,
	 *  not because it resembled anything. Both knobs are adjustable and both are shown — a silent
	 *  threshold is how a corpus gets mislabelled at scale (`open_browse.md` §5). */
	let maxDistance = $state(DEFAULT_MAX_DISTANCE);

	/** Loaded only once someone asks. A browse page nobody is building a batch on should not fan out
	 *  a vector search per chunk it renders. */
	const neighbours = $derived(
		open && seedKey ? findSimilar({ key: seedKey, dataset, table, n: howMany }) : null,
	);

	const returned = $derived(
		(neighbours?.current?.ok ? neighbours.current.data : []) as SimilarHit[],
	);
	/** The cutoff is applied CLIENT-side, on what the service returned: the slider must react without
	 *  a round trip, or dragging it is guesswork. `howMany` is the server-side ask. */
	const hits = $derived(withinDistance(returned, maxDistance));
	const bounds = $derived(distanceBounds(returned));
	const cutoffSummary = $derived(describeCutoff(returned.length, hits.length));

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
			<!-- THE TWO KNOBS, both visible and both adjustable (`open_browse.md` §5). The plan is
			     explicit that this is the difference between a useful tool and one that silently
			     mislabels a corpus, and the summary line is the visible half: it names how many
			     candidates the cutoff EXCLUDED, because "18 of 24" leaves the six invisible and the
			     dropped count is what tells you the cutoff is too strict. -->
			<div class="border-border flex flex-col gap-1.5 border-b pb-2" data-testid="propagate-knobs">
				<div class="flex items-center gap-2 text-xs">
					<label class="text-muted-foreground shrink-0" for="similar-n">neighbours</label>
					<Input
						id="similar-n"
						type="number"
						min="1"
						max={MAX_SIMILAR_N}
						bind:value={howMany}
						class="h-7 w-20 text-xs"
						data-testid="propagate-n"
					/>
					{#if bounds}
						<span class="text-muted-foreground ml-auto font-mono">
							{bounds.nearest.toFixed(3)}–{bounds.furthest.toFixed(3)}
						</span>
					{/if}
				</div>
				{#if bounds}
					<!-- Bounded to the distances ACTUALLY returned, so the control cannot be dragged into
					     a range where nothing lives — a slider that does nothing is worse than none. -->
					<div class="flex items-center gap-2 text-xs">
						<label class="text-muted-foreground shrink-0" for="similar-cutoff">cutoff</label>
						<input
							id="similar-cutoff"
							type="range"
							class="h-7 flex-1"
							min={bounds.nearest}
							max={bounds.furthest}
							step="0.001"
							bind:value={maxDistance}
							data-testid="propagate-cutoff"
						/>
						<span class="font-mono">{Number(maxDistance).toFixed(3)}</span>
					</div>
				{/if}
				<p class="text-muted-foreground text-xs" data-testid="propagate-summary">
					{cutoffSummary}
				</p>
			</div>

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
