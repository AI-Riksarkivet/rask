<script lang="ts">
	// WHICH corpus am I looking at, and what else is there?
	//
	// The zone could always serve any dataset — `descriptor-store` reads `?dataset=<id>` and the
	// backend has listed them at `GET /api/datasets` the whole time — but nothing said so on screen,
	// so choosing one meant knowing the mechanism and hand-editing the URL.
	//
	// Real links, not a click handler that pushes state: each option is where you would GO, so it
	// middle-clicks into a tab and copies like any other address. `data-sveltekit-reload` because
	// switching datasets must re-run the descriptor load — the store loads once on mount and every
	// renderer reads the module-level active view, so a soft nav would leave the previous corpus's
	// descriptor in place while the URL claimed otherwise.
	import { Database } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import { listDatasets } from '@rask/explorer-api';

	import {
		datasetChoices,
		datasetHref,
		tableChoices,
		tableHref,
		type DatasetChoice,
		type SearchTableLike,
	} from '$lib/dataset-choice';

	let {
		activeId,
		defaultId = null,
		url,
		searchTables = [],
	}: {
		/** What the descriptor actually loaded — not the query param; they differ on the default. */
		activeId: string | null;
		/** The backend's default DB id, which is reached by having NO `?dataset=` at all. */
		defaultId?: string | null;
		url: URL;
		/** The active corpus's declared searchable tables. A corpus may expose several. */
		searchTables?: SearchTableLike[];
	} = $props();

	/** The table section. DERIVED from the URL, not fetched: the corpus's declared tables arrive with
	 *  the descriptor the layout already loaded, so there is nothing to go and ask for. */
	const tables = $derived(tableChoices(searchTables, url.searchParams.get('table')));
	/** One table is not a choice — showing a section with a single row would be furniture. */
	const showTables = $derived(tables.length > 1);
	const activeTable = $derived(tables.find((t) => t.active) ?? null);

	let choices = $state<DatasetChoice[]>([]);
	let error = $state('');
	let loaded = $state(false);

	/** Fetched on OPEN, not on mount: the search page already makes several requests before it can
	 *  paint, and a corpus list nobody has asked to see should not be one of them. */
	async function load(): Promise<void> {
		if (loaded) return;
		try {
			choices = datasetChoices(await listDatasets(), activeId, defaultId);
			error = '';
		} catch (err) {
			// Named, not swallowed: an unreachable listing and a backend serving exactly one corpus
			// both leave the menu empty, and only one of them is worth telling someone about.
			error = String(err);
		}
		loaded = true;
	}

	/** Corpus, plus the table when the corpus offers more than one — so the trigger always says
	 *  exactly what is being searched rather than only half of it. */
	const label = $derived(
		showTables && activeTable
			? `${activeId ?? 'dataset'} · ${activeTable.name}`
			: (activeId ?? 'dataset'),
	);
</script>

<Popover.Root onOpenChange={(open) => open && void load()}>
	<Popover.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				size="sm"
				class="gap-1.5"
				data-testid="dataset-picker"
				title="the corpus this zone is looking at"
			>
				<Database class="size-3.5" />
				<span class="max-w-[14rem] truncate font-mono text-xs">{label}</span>
			</Button>
		{/snippet}
	</Popover.Trigger>
	<Popover.Content class="w-80 p-1" align="start">
		<!-- TABLES first: the narrower choice, and the one already answerable — the corpus's declared
		     tables ride in the descriptor the layout loaded, so this section needs no fetch and is
		     there the instant the popover opens. Hidden when the corpus declares one, because a
		     section with a single row is furniture rather than a choice. -->
		{#if showTables}
			<div class="text-muted-foreground px-2 pt-1 pb-0.5 text-[10px] font-medium uppercase">Table</div>
			<ul class="flex flex-col" data-testid="table-list">
				{#each tables as table (table.name)}
					<li>
						<a
							href={tableHref(url, table)}
							data-sveltekit-reload
							data-testid="table-option"
							data-active={table.active}
							class="hover:bg-accent flex items-center gap-2 rounded-sm px-2 py-1.5 no-underline
								{table.active ? 'bg-accent/60' : ''}"
						>
							<span class="font-mono text-xs">{table.name}</span>
							{#if table.name !== table.rowTable}
								<span class="text-muted-foreground text-[11px]">{table.rowTable}</span>
							{/if}
							{#if table.active}
								<Badge variant="outline" class="ml-auto py-0 text-[10px]">current</Badge>
							{/if}
						</a>
					</li>
				{/each}
			</ul>
			<div class="border-border my-1 border-t"></div>
			<div class="text-muted-foreground px-2 pb-0.5 text-[10px] font-medium uppercase">Corpus</div>
		{/if}
		{#if error}
			<p class="text-destructive px-2 py-1.5 text-xs" data-testid="dataset-picker-error">{error}</p>
		{:else if !loaded}
			<p class="text-muted-foreground px-2 py-1.5 text-xs">Loading corpora…</p>
		{:else if choices.length === 0}
			<p class="text-muted-foreground px-2 py-1.5 text-xs">This backend serves no other corpus.</p>
		{:else}
			<ul class="flex flex-col">
				{#each choices as choice (choice.id)}
					<li>
						<a
							href={datasetHref(url, choice)}
							data-sveltekit-reload
							data-testid="dataset-option"
							data-active={choice.active}
							class="hover:bg-accent flex flex-col gap-0.5 rounded-sm px-2 py-1.5 no-underline
								{choice.active ? 'bg-accent/60' : ''}"
						>
							<span class="flex items-center gap-2">
								<span class="font-mono text-xs">{choice.id}</span>
								{#if choice.active}
									<Badge variant="outline" class="py-0 text-[10px]">current</Badge>
								{/if}
							</span>
							<!-- The separator is INSIDE the text expression, not adjacent markup: svelte trims
							     whitespace around a block tag, so `rows{#if …} · …{/if}` rendered as
							     "2 rows· frames". Caught in the browser, not by reading. -->
							<span class="text-muted-foreground text-[11px]">
								{choice.rows.toLocaleString()} rows{choice.capabilities.length > 0
									? ` · ${choice.capabilities.join(', ')}`
									: ''}
							</span>
						</a>
					</li>
				{/each}
			</ul>
		{/if}
	</Popover.Content>
</Popover.Root>
