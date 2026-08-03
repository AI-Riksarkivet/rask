<!--
  Navigation trail for the graph explorer: Overview › entity › entity › current.
  Single-file take on the shadcn-svelte Breadcrumb (same markup semantics +
  aria contract) to match this repo's flat ui/ component style. Long trails
  collapse to "Overview › … › last two"; clicking the ellipsis expands.
-->
<script lang="ts">
	import { ChevronRight, Ellipsis } from '@lucide/svelte';

	interface Crumb {
		id: string;
		name: string;
	}

	let {
		trail,
		onNavigate,
	}: {
		trail: Crumb[];
		onNavigate: (id: string | null) => void;
	} = $props();

	const MAX_VISIBLE = 3; // crumbs shown after "Overview" before collapsing
	// expansion is tied to the trail array's identity: every navigation assigns
	// a fresh array in the parent, so the ellipsis folds back up automatically
	let expandedFor = $state<Crumb[] | null>(null);
	const expanded = $derived(expandedFor === trail);

	const hidden = $derived(expanded || trail.length <= MAX_VISIBLE ? [] : trail.slice(0, -2));
	const visible = $derived(expanded || trail.length <= MAX_VISIBLE ? trail : trail.slice(-2));
</script>

<nav aria-label="breadcrumb" class="min-w-0">
	<ol class="text-muted-foreground flex flex-wrap items-center gap-1 text-sm">
		<li>
			{#if trail.length === 0}
				<span aria-current="page" class="text-foreground font-medium">Overview</span>
			{:else}
				<button class="hover:text-foreground transition-colors" onclick={() => onNavigate(null)}>
					Overview
				</button>
			{/if}
		</li>
		{#if hidden.length > 0}
			<li aria-hidden="true"><ChevronRight class="size-3.5" /></li>
			<li>
				<button
					class="hover:text-foreground flex items-center transition-colors"
					aria-label="Show full path"
					onclick={() => (expandedFor = trail)}
				>
					<Ellipsis class="size-4" />
				</button>
			</li>
		{/if}
		{#each visible as crumb, i (crumb.id)}
			<li aria-hidden="true"><ChevronRight class="size-3.5" /></li>
			<li class="min-w-0">
				{#if i === visible.length - 1}
					<span aria-current="page" class="text-foreground block max-w-48 truncate font-medium">
						{crumb.name}
					</span>
				{:else}
					<button
						class="hover:text-foreground block max-w-40 truncate transition-colors"
						onclick={() => onNavigate(crumb.id)}
					>
						{crumb.name}
					</button>
				{/if}
			</li>
		{/each}
	</ol>
</nav>
