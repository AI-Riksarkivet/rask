<script lang="ts">
	/** The execution node. Searches for its inline query (or a connected Query
	 *  node / Image), in its chosen mode. If another Search feeds into it, this
	 *  one is scoped to the videos that upstream result came from — so chaining
	 *  Search → Search refines progressively. Two input ports: "in" (query /
	 *  filter / upstream results) and "image" (Image node only), so the wires
	 *  show what feeds what. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import type { SearchMode } from '@rask/explorer-api';
	import {
		graph,
		SEARCH_MODES,
		modeLabel,
		DEFAULT_N,
		MAX_N,
		MIN_N,
		type NodeKind,
		type RefineScope,
	} from '$lib/workflow/graph.svelte';
	import { SEARCH_IMAGE_HANDLE, SEARCH_IN_HANDLE } from '$lib/workflow/types';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
	const isVisual = $derived(cfg?.mode === 'visual');

	const REFINE_SCOPES: { value: RefineScope; label: string }[] = [
		{ value: 'video', label: 'Videos' },
		{ value: 'chunk', label: 'Chunks' },
	];

	// The refine scope only does anything when a RESULT set feeds this node (an
	// upstream Search/Combine/Tagger). Without one, the toggle is a no-op — hide
	// it so it isn't set on the wrong node (e.g. an image-only first Search).
	const RESULT_KINDS: NodeKind[] = ['search', 'combine', 'tagger'];
	const hasUpstreamResults = $derived(
		graph.edges.some((e) => {
			const k = graph.kindOf(e.source);
			return e.target === id && k !== null && RESULT_KINDS.includes(k);
		}),
	);
</script>

{#if cfg && rt}
	<NodeShell {id} title="Search · {modeLabel(cfg.mode)}" status={rt.status} {selected}>
		<!-- Two target ports (handles + labels anchor to the xyflow node wrapper).
         The image port is violet to match the image-edge stroke colour. -->
		<Handle
			id={SEARCH_IN_HANDLE}
			type="target"
			position={Position.Left}
			style="top: 35%"
			title="Query · filter · upstream results"
		/>
		<Handle
			id={SEARCH_IMAGE_HANDLE}
			type="target"
			position={Position.Left}
			style="top: 65%; background: #8b5cf6; border-color: #8b5cf6;"
			title="Image (wire an Image node here)"
		/>
		<span
			class="text-muted-foreground pointer-events-none absolute left-1.5 -translate-y-1/2 text-[8px] leading-none"
			style="top: 35%">in</span
		>
		<span
			class="pointer-events-none absolute left-1.5 -translate-y-1/2 text-[8px] leading-none text-violet-400"
			style="top: 65%">img</span
		>

		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="q-{id}">
			{isVisual ? 'Query (optional — image drives it)' : 'Query'}
		</label>
		<input
			id="q-{id}"
			class="{FIELD_CLASS} w-full"
			placeholder={isVisual ? 'uses the connected image' : 'e.g. skatt'}
			bind:value={cfg.q}
		/>

		<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="mode-{id}">Mode</label>
		<select
			id="mode-{id}"
			class="{FIELD_CLASS} w-full"
			value={cfg.mode}
			onchange={(e) => graph.setConfig(id, { mode: e.currentTarget.value as SearchMode })}
		>
			{#each SEARCH_MODES as m (m.value)}
				<option value={m.value}>{m.label}</option>
			{/each}
		</select>

		<div class="mt-2 flex items-center gap-2">
			<label class="text-muted-foreground text-[0.7rem]" for="n-{id}">Results</label>
			<input
				id="n-{id}"
				type="number"
				min={MIN_N}
				max={MAX_N}
				class="{FIELD_CLASS} w-16"
				value={cfg.n}
				oninput={(e) =>
					graph.setConfig(id, {
						n: Math.max(MIN_N, Math.min(MAX_N, Number(e.currentTarget.value) || DEFAULT_N)),
					})}
			/>
			<label class="nodrag text-muted-foreground ml-auto flex items-center gap-1.5 text-[0.7rem]">
				<input type="checkbox" bind:checked={cfg.rerank} />
				Rerank
			</label>
		</div>

		<div class="mt-2 flex items-center gap-2">
			<label
				class="text-muted-foreground text-[0.7rem]"
				for="min-score-{id}"
				title="Drop hits scoring below this (normalized, higher = better). Empty = no threshold."
			>
				Min score
			</label>
			<input
				id="min-score-{id}"
				type="number"
				step="any"
				class="{FIELD_CLASS} w-16"
				placeholder="off"
				value={cfg.minScore ?? ''}
				oninput={(e) => {
					const raw = e.currentTarget.value.trim();
					const num = Number(raw);
					graph.setConfig(id, { minScore: raw === '' || Number.isNaN(num) ? null : num });
				}}
			/>
		</div>

		{#if hasUpstreamResults}
			<div class="nodrag text-muted-foreground mt-2 flex items-center gap-1.5 text-[0.7rem]">
				<span
					title="Re-rank all chunks in the upstream videos, or narrow to the exact upstream chunks"
				>
					Refine within
				</span>
				<div class="border-border ml-auto flex overflow-hidden rounded-md border">
					{#each REFINE_SCOPES as s (s.value)}
						<button
							type="button"
							class="px-1.5 py-0.5 transition-colors {cfg.refineScope === s.value
								? 'bg-primary text-primary-foreground'
								: 'hover:bg-muted'}"
							onclick={() => graph.setConfig(id, { refineScope: s.value })}
						>
							{s.label}
						</button>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Run summary; on error the NodeShell banner carries the signal, so the
         whole footer (divider included) is hidden rather than left blank. -->
		{#if rt.status !== 'error'}
			<div class="border-border mt-2 border-t pt-1.5 text-[0.7rem]">
				{#if rt.status === 'running'}
					<span class="text-primary">Searching…</span>
				{:else if rt.status === 'done'}
					<span class="text-muted-foreground">
						<span class="text-foreground">{rt.count}</span> hits · {rt.ms} ms{#if rt.scopedDocs}
							· within <span class="text-foreground">{rt.scopedDocs}</span>
							videos{#if rt.scopeCapped}
								<span class="text-amber-500"> (capped)</span>{/if}{:else if rt.scopedChunks}
							· within <span class="text-foreground">{rt.scopedChunks}</span>
							chunks{#if rt.scopeCapped}
								<span class="text-amber-500"> (capped)</span>{/if}{/if}
					</span>
					{#if rt.count === 0 && (rt.scopedDocs || rt.scopedChunks)}
						<div class="mt-1 text-amber-500">
							Nothing matched inside the {rt.scopedChunks
								? `${rt.scopedChunks} chunks`
								: `${rt.scopedDocs} videos`}
							— delete the incoming refine edge to search all.
						</div>
					{/if}
				{:else}
					<span class="text-muted-foreground/70">idle — add a query or image, then Run</span>
				{/if}
				{#if rt.droppedInputs > 0}
					<div class="mt-1 text-amber-500">
						{rt.droppedInputs} extra input{rt.droppedInputs > 1 ? 's' : ''} ignored — a Search uses one
						query + one image. Use a Combine node to merge result sets.
					</div>
				{/if}
			</div>
		{/if}

		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
