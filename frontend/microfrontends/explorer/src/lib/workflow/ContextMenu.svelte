<script lang="ts">
	/** Right-click menu for a node (duplicate / disable / delete) or the pane
	 *  (add a node at the cursor). Self-contained: reads/acts on the graph; the
	 *  FlowPane owns the open/position state and the dismiss handlers. */
	import { Copy, Eye, EyeOff, Play, Plus, RefreshCw, Trash2 } from '@lucide/svelte';
	import { graph, NODE_KINDS, nodeLabel } from '$lib/workflow/graph.svelte';

	interface Menu {
		x: number;
		y: number;
		mode: 'node' | 'pane';
		nodeId: string | null;
		/** Flow-space drop point for pane "add" actions. */
		flow: { x: number; y: number };
	}

	let { menu, onClose }: { menu: Menu; onClose: () => void } = $props();

	const cfg = $derived(menu.nodeId ? graph.config[menu.nodeId] : null);

	const ITEM =
		'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left transition-colors hover:bg-muted';

	function act(fn: () => void): void {
		fn();
		onClose();
	}
</script>

<!-- Escape dismisses the menu (role="menu" keyboard contract). -->
<svelte:window
	onkeydown={(e) => {
		if (e.key === 'Escape') onClose();
	}}
/>

<!-- Backdrop: any click (or another right-click) dismisses the menu. -->
<div
	class="fixed inset-0 z-40"
	role="presentation"
	onclick={onClose}
	oncontextmenu={(e) => {
		e.preventDefault();
		onClose();
	}}
></div>

<div
	class="border-border bg-card text-foreground fixed z-50 min-w-[9rem] rounded-md border p-1 text-xs shadow-lg"
	style="left: {menu.x}px; top: {menu.y}px;"
	role="menu"
	tabindex="-1"
	{@attach (el) => el.focus()}
>
	{#if menu.mode === 'node' && menu.nodeId && cfg}
		<button
			class={ITEM}
			role="menuitem"
			disabled={graph.running}
			onclick={() => act(() => void graph.runNode(menu.nodeId!))}
		>
			<Play class="size-3.5" /> Run node
		</button>
		<button
			class={ITEM}
			role="menuitem"
			disabled={graph.running}
			title="Re-execute this node AND everything upstream of it"
			onclick={() => act(() => void graph.runNode(menu.nodeId!, { fresh: true }))}
		>
			<RefreshCw class="size-3.5" /> Run branch fresh
		</button>
		<button
			class={ITEM}
			role="menuitem"
			onclick={() => act(() => graph.duplicateNode(menu.nodeId!))}
		>
			<Copy class="size-3.5" /> Duplicate
		</button>
		<button
			class={ITEM}
			role="menuitem"
			disabled={graph.running}
			onclick={() => act(() => graph.setConfig(menu.nodeId!, { enabled: !cfg.enabled }))}
		>
			{#if cfg.enabled}<EyeOff class="size-3.5" /> Disable{:else}<Eye class="size-3.5" /> Enable{/if}
		</button>
		<button
			class="{ITEM} text-destructive"
			role="menuitem"
			onclick={() => act(() => graph.removeNode(menu.nodeId!))}
		>
			<Trash2 class="size-3.5" /> Delete
		</button>
	{:else}
		<div class="text-muted-foreground px-2 py-1 text-[0.7rem] tracking-wide uppercase">
			Add node
		</div>
		{#each NODE_KINDS as kind (kind)}
			<button
				class={ITEM}
				role="menuitem"
				onclick={() => act(() => graph.addNode(kind, menu.flow))}
			>
				<Plus class="size-3.5" />
				{nodeLabel(kind)}
			</button>
		{/each}
	{/if}
</div>
