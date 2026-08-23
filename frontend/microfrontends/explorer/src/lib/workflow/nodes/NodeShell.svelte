<script lang="ts">
	/** Shared card chrome for every workflow node: a titled surface with a
	 *  run-status dot and hover-revealed actions (duplicate / disable / delete).
	 *  Title falls back to the node's custom `label` when set. */
	import type { Snippet } from 'svelte';
	import { Copy, Eye, EyeOff, Play, X } from '@lucide/svelte';
	import { graph, STATUS_DOT, type RunStatus } from '$lib/workflow/graph.svelte';

	let {
		id,
		title,
		status = 'idle',
		selected = false,
		width = 'w-64',
		children,
	}: {
		id: string;
		title: string;
		status?: RunStatus;
		selected?: boolean;
		width?: string;
		children: Snippet;
	} = $props();

	const cfg = $derived(graph.config[id]);
	const displayTitle = $derived(cfg?.label?.trim() || title);
	const enabled = $derived(cfg?.enabled ?? true);
	// "stale" = upstream re-ran since this node's results (runtime flag), OR
	// this node itself was edited/rewired since it last ran (live fingerprint).
	const stale = $derived((graph.runtime[id]?.stale ?? false) || graph.isOutdated(id));

	// Per-node error attribution: when this node's run failed, surface the message
	// right on the card (the red status dot alone isn't enough to debug from).
	// Shown for EVERY node kind, so attribution is consistent, not Search-only.
	const error = $derived(status === 'error' ? (graph.runtime[id]?.error ?? null) : null);

	const btn =
		'nodrag shrink-0 rounded-md p-0.5 text-muted-foreground/40 opacity-0 transition-opacity group-hover:opacity-100 hover:text-foreground';
</script>

<div
	class="group {width} border-border bg-card rounded-lg border shadow-sm transition-all"
	class:ring-2={selected}
	class:ring-primary={selected}
	class:opacity-60={!enabled}
>
	<div class="border-border flex items-center gap-1.5 border-b px-3 py-1.5">
		<span class="size-2 shrink-0 rounded-full {STATUS_DOT[status]}"></span>
		<span class="text-foreground min-w-0 flex-1 truncate text-xs font-semibold" title={displayTitle}
			>{displayTitle}</span
		>
		{#if !enabled}
			<span
				class="bg-muted text-muted-foreground shrink-0 rounded-md px-1 text-[9px] tracking-wide uppercase"
				>off</span
			>
		{/if}
		{#if stale}
			<span
				class="shrink-0 rounded-md bg-amber-500/15 px-1 text-[9px] tracking-wide text-amber-600 uppercase dark:text-amber-400"
				title="This node's results are out of date (it was edited, rewired, or upstream re-ran) — press ▶ to refresh"
				>stale</span
			>
		{/if}
		<button
			class="{btn} disabled:opacity-30"
			title="Run this node — reuses upstream results, runs missing upstream once (Shift: rerun the whole branch)"
			aria-label="Run node"
			disabled={graph.running}
			onclick={(e) => {
				e.stopPropagation();
				void graph.runNode(id, { fresh: e.shiftKey });
			}}
		>
			<Play class="size-3.5" />
		</button>
		<button
			class={btn}
			title="Duplicate node"
			aria-label="Duplicate node"
			onclick={(e) => {
				e.stopPropagation();
				graph.duplicateNode(id);
			}}
		>
			<Copy class="size-3.5" />
		</button>
		<button
			class="{btn} disabled:opacity-30"
			title={enabled ? 'Disable (bypass) node' : 'Enable node'}
			aria-label="Toggle node enabled"
			disabled={graph.running}
			onclick={(e) => {
				e.stopPropagation();
				graph.setConfig(id, { enabled: !enabled });
			}}
		>
			{#if enabled}<EyeOff class="size-3.5" />{:else}<Eye class="size-3.5" />{/if}
		</button>
		<button
			class="{btn} hover:bg-destructive/15 hover:text-destructive"
			title="Delete node"
			aria-label="Delete node"
			onclick={(e) => {
				e.stopPropagation();
				graph.removeNode(id);
			}}
		>
			<X class="size-3.5" />
		</button>
	</div>
	<div class="text-foreground px-3 py-2 text-xs">
		{#if error}
			<div
				class="nodrag border-destructive/30 bg-destructive/10 text-destructive mb-2 max-h-16 overflow-y-auto rounded-md border px-2 py-1 text-[0.7rem] leading-snug break-words"
				title={error}
			>
				{error}
			</div>
		{/if}
		{@render children()}
	</div>
</div>
