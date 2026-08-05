<script lang="ts">
	/** The node library — the flow builder's palette, living in the ZONE RAIL under the route list
	 *  (the shell's `sidebarContent` seam, the same one the lakehouse workbench's saved-views list
	 *  uses). Search, then drag a row onto the canvas or click it to drop one at the viewport centre.
	 *
	 *  It is grouped the way the platform thinks: the LIVE Ray Serve deployments first — each row
	 *  drops a Model node already pointed at that app — then the editor's own input/inference/
	 *  transform/output kinds. Deliberately store-free (see palette.ts): it talks to the canvas
	 *  through a DataTransfer payload or a window event, never through the graph singleton, so the
	 *  shared layout can mount it without instantiating the graph on every studio route. */
	import { RefreshCw, Search, Zap } from '@lucide/svelte';
	import { getServeApps } from '$lib/flows/remote/serve.remote';
	import {
		ADD_NODE_EVENT,
		encodeDrop,
		filterGroups,
		NODE_DND_MIME,
		serveGroup,
		STATIC_GROUPS,
		type PaletteDrop,
		type PaletteEntry,
	} from '$lib/flows/palette';

	const appsQuery = getServeApps();
	const discovery = $derived(appsQuery.current);

	// null = not answered yet OR discovery failed; the group renders its own note either way.
	const groups = $derived.by(() => {
		const serve = discovery?.ok
			? serveGroup(discovery.apps)
			: serveGroup(null, discovery && !discovery.ok ? discovery.detail : undefined);
		return filterGroups([serve, ...STATIC_GROUPS], query);
	});

	let query = $state('');

	function onDragStart(e: DragEvent, drop: PaletteDrop): void {
		if (!e.dataTransfer) return;
		e.dataTransfer.setData(NODE_DND_MIME, encodeDrop(drop));
		e.dataTransfer.effectAllowed = 'move';
	}

	/** Click-to-add: the canvas owns positioning (only it knows where the viewport centre is), so
	 *  the rail just announces the intent. */
	function onPick(entry: PaletteEntry): void {
		const drop: PaletteDrop = {
			kind: entry.kind,
			...(entry.config ? { config: entry.config } : {}),
		};
		window.dispatchEvent(new CustomEvent(ADD_NODE_EVENT, { detail: drop }));
	}
</script>

<div class="flex min-h-0 flex-col gap-2 px-2 group-data-[collapsible=icon]:hidden">
	<div class="flex items-center justify-between gap-1">
		<span class="text-muted-foreground text-[0.7rem] font-medium tracking-wide uppercase">
			Nodes
		</span>
		<button
			class="text-muted-foreground/60 hover:text-foreground rounded p-0.5"
			title="Refresh the live Serve app list"
			aria-label="Refresh Serve apps"
			onclick={() => void appsQuery.refresh().catch(() => {})}
		>
			<RefreshCw class="size-3" />
		</button>
	</div>

	<div class="relative">
		<Search
			class="text-muted-foreground/60 pointer-events-none absolute top-1/2 left-2 size-3 -translate-y-1/2"
		/>
		<input
			class="border-border bg-background text-foreground focus:border-primary w-full rounded border py-1 pr-2 pl-6 text-xs outline-none"
			placeholder="Search nodes…"
			aria-label="Search nodes"
			bind:value={query}
		/>
	</div>

	<div class="flex min-h-0 flex-col gap-2 overflow-y-auto">
		{#each groups as group (group.label)}
			<div class="flex flex-col gap-1">
				<span
					class="text-muted-foreground/70 px-1 text-[0.65rem] font-medium tracking-wide uppercase"
				>
					{group.label}
				</span>
				{#each group.entries as entry (entry.label)}
					<button
						type="button"
						draggable="true"
						ondragstart={(e) => onDragStart(e, entry)}
						onclick={() => onPick(entry)}
						title="{entry.blurb} — drag onto the canvas, or click to add"
						class="border-border bg-background hover:bg-muted flex w-full cursor-grab flex-col items-start gap-0.5 rounded-md border px-2 py-1 text-left transition-colors active:cursor-grabbing"
					>
						<span class="text-foreground flex w-full items-center gap-1 text-xs">
							{#if entry.live}
								<Zap class="size-3 shrink-0 text-emerald-500" />
							{/if}
							<span class="truncate">{entry.label}</span>
						</span>
						<span class="text-muted-foreground w-full truncate text-[0.65rem]">{entry.blurb}</span>
					</button>
				{/each}
				{#if group.entries.length === 0 && group.emptyNote}
					<p class="text-muted-foreground/70 px-1 text-[0.65rem] leading-snug">{group.emptyNote}</p>
				{/if}
			</div>
		{/each}
		{#if groups.length === 0}
			<p class="text-muted-foreground/70 px-1 text-[0.65rem]">No node matches “{query}”.</p>
		{/if}
	</div>
</div>
