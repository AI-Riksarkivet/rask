<script lang="ts">
	// Filter the RENDERED graph — no refetch. Every count is computed from the unfiltered graph, so a
	// facet that is currently off still shows what turning it on would bring back; counts that shrink
	// to match the filter can never be used to undo it.
	import { Button } from '@rask/ui/button';
	import { Checkbox } from '@rask/ui/checkbox';
	import { Filter, RotateCcw } from '@lucide/svelte';

	type Props = {
		typeCounts: ReadonlyMap<string, number>;
		relationCounts: ReadonlyMap<string, number>;
		selectedTypes: Set<string>;
		selectedRelations: Set<string>;
		shown: number;
		total: number;
		onchange: (next: { types: Set<string>; relations: Set<string> }) => void;
	};

	let {
		typeCounts,
		relationCounts,
		selectedTypes,
		selectedRelations,
		shown,
		total,
		onchange,
	}: Props = $props();

	const types = $derived([...typeCounts.entries()].toSorted((a, b) => a[0].localeCompare(b[0])));
	// Relations are ranked by frequency: `parent` dwarfs everything in this model, and burying the rung
	// an operator is actually looking for under an alphabetical list is a worse default than density.
	const relations = $derived(
		[...relationCounts.entries()].toSorted((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
	);
	const filtering = $derived(selectedTypes.size > 0 || selectedRelations.size > 0);

	// A plain Set, deliberately — NOT SvelteSet. This never mutates: it returns a fresh Set that the
	// parent REASSIGNS onto its `$state`, and reassignment is what makes that state fire. `$state` does
	// not proxy Set methods anyway, so a mutated Set would be the silent-no-update bug; SvelteSet would
	// buy reactivity this component does not use and hide that the value is replaced, not edited.
	function toggle(set: Set<string>, key: string): Set<string> {
		const next = new Set(set);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		return next;
	}
</script>

<div class="flex max-h-96 flex-col gap-4 overflow-y-auto" data-slot="facet-rail">
	<div class="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider">
		<Filter size={12} /> Filter
	</div>

	<section class="flex flex-col gap-1">
		<h3 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Type</h3>
		<!-- A DIV, not a <label>. Bits UI renders its checkbox as a <button role="checkbox">, and <button>
		     is a labelable element — so a wrapping <label> forwards the click back to the control and
		     every direct click toggled TWICE, netting no change at all. `for`/`id` association keeps the
		     text clickable without the second fire. Caught by the e2e facet spec. -->
		{#each types as [type, count] (type)}
			<div class="flex items-center gap-2 py-0.5 text-xs">
				<Checkbox
					id="facet-type-{type}"
					checked={selectedTypes.has(type)}
					onCheckedChange={() =>
						onchange({ types: toggle(selectedTypes, type), relations: selectedRelations })}
					aria-label="Filter by type {type}"
				/>
				<label for="facet-type-{type}" class="flex-1 cursor-pointer truncate font-mono"
					>{type}</label
				>
				<span class="tabular-nums text-muted-foreground">{count}</span>
			</div>
		{:else}
			<p class="text-xs text-muted-foreground">Nothing on the canvas yet.</p>
		{/each}
	</section>

	<section class="flex flex-col gap-1">
		<h3 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
			Relation
		</h3>
		{#each relations as [relation, count] (relation)}
			<div class="flex items-center gap-2 py-0.5 text-xs">
				<Checkbox
					id="facet-relation-{relation}"
					checked={selectedRelations.has(relation)}
					onCheckedChange={() =>
						onchange({ types: selectedTypes, relations: toggle(selectedRelations, relation) })}
					aria-label="Filter by relation {relation}"
				/>
				<label for="facet-relation-{relation}" class="flex-1 cursor-pointer truncate font-mono">
					{relation}
				</label>
				<span class="tabular-nums text-muted-foreground">{count}</span>
			</div>
		{/each}
	</section>

	<div class="mt-auto flex flex-col gap-2 pt-2 text-xs text-muted-foreground">
		<span class="tabular-nums">
			{shown} of {total} node{total === 1 ? '' : 's'} shown
		</span>
		{#if filtering}
			<Button
				size="sm"
				variant="outline"
				onclick={() => onchange({ types: new Set(), relations: new Set() })}
			>
				<RotateCcw size={12} /> Clear filters
			</Button>
		{/if}
	</div>
</div>
