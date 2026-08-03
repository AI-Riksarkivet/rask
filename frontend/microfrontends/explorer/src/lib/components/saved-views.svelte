<script lang="ts">
	// Saved-views control (next to the search bar): save the current query as a named view,
	// and re-apply / delete saved ones. Thin view over the savedViews store; the query
	// logic lives in $lib/saved-views. Views are scoped to the active dataset.
	//
	// A bits-ui Popover, like the zone's other header menus (Settings, Filters, Help), so
	// the four triggers share one open/close behaviour and one surface treatment.
	import { Bookmark, Check, X } from '@lucide/svelte';
	import { Popover } from 'bits-ui';
	import { activeView, type SearchSpec } from '@rask/explorer-api';
	import { savedViews } from '$lib/saved-views.svelte';
	import { Badge, Button, buttonVariants } from '@rask/ui';
	import { Input } from '@rask/ui/input';

	let { spec, onapply }: { spec: SearchSpec; onapply: (s: SearchSpec) => void } = $props();

	let open = $state(false);
	let name = $state('');
	const dataset = $derived(activeView().datasetParam() ?? '');
	const views = $derived(savedViews.forDataset(dataset));

	// The store loads once per session; mounting it here rather than in the layout keeps the fetch with
	// the only surface that reads it. `load()` is idempotent, so a second popover mount just re-reads.
	$effect(() => {
		if (!savedViews.ready && savedViews.unreadable === null) void savedViews.load();
	});

	async function save(): Promise<void> {
		if (!name.trim()) return;
		await savedViews.save(name, spec, dataset);
		name = '';
	}
	function apply(s: SearchSpec): void {
		onapply({ ...s });
		open = false;
	}
</script>

<Popover.Root bind:open>
	<Popover.Trigger class={buttonVariants({ variant: 'outline', size: 'sm' })} title="Saved views">
		<Bookmark />
		Views
		{#if views.length}
			<Badge variant="secondary" class="px-1.5 py-0 text-[0.7rem]">{views.length}</Badge>
		{/if}
	</Popover.Trigger>

	<Popover.Portal>
		<Popover.Content
			sideOffset={6}
			align="end"
			class="border-border bg-popover text-popover-foreground z-50 w-64 rounded-lg border p-2 text-xs shadow-md"
		>
			<div class="flex gap-1">
				<Input
					bind:value={name}
					placeholder="Save current view as…"
					class="h-7 rounded-lg text-xs"
					onkeydown={(e) => e.key === 'Enter' && save()}
				/>
				<Button
					size="icon-sm"
					disabled={!name.trim() || !savedViews.ready}
					onclick={save}
					title={savedViews.ready ? 'Save view' : 'Saving is disabled until your views load'}
				>
					<Check />
				</Button>
			</div>

			{#if savedViews.unreadable}
				<!-- The point of the whole change: an empty list and an unreadable one look identical, and
				     only one of them means the user's views are still there. Saving stays disabled above, so
				     nothing can be written over a record we could not read. -->
				<p class="text-destructive mt-2 text-xs">
					{savedViews.unreadable}
				</p>
			{:else if views.length}
				<ul class="mt-2 flex flex-col gap-0.5">
					{#each views as v (v.name)}
						<li class="hover:bg-muted/60 flex items-center gap-1 rounded-md">
							<button
								class="flex-1 truncate px-2 py-1 text-left text-xs"
								title={v.spec.q || '(browse)'}
								onclick={() => apply(v.spec)}
							>
								{v.name}
							</button>
							<Button
								variant="ghost"
								size="icon-xs"
								class="hover:text-destructive"
								title="Delete view"
								aria-label="Delete view {v.name}"
								onclick={() => void savedViews.remove(v.name, dataset)}
							>
								<X />
							</Button>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="text-muted-foreground mt-2 px-1 text-xs">No saved views yet.</p>
			{/if}
		</Popover.Content>
	</Popover.Portal>
</Popover.Root>
