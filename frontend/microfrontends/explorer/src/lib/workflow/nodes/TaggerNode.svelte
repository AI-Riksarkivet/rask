<script lang="ts">
	/** Tagger node: stamps its tags onto every hit flowing through it (writing the
	 *  shared tag store), then forwards them unchanged. Those tags ride into Export
	 *  and show on the chunk everywhere it appears. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { X } from '@lucide/svelte';
	import { graph } from '$lib/workflow/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);

	let draft = $state('');

	function addTag(): void {
		if (!cfg) return;
		const t = draft.trim();
		draft = '';
		if (!t || cfg.tags.includes(t)) return;
		graph.setConfig(id, { tags: [...cfg.tags, t] });
	}

	function removeTag(tag: string): void {
		if (!cfg) return;
		graph.setConfig(id, { tags: cfg.tags.filter((t) => t !== tag) });
	}
</script>

{#if cfg && rt}
	<NodeShell {id} title="Tagger" status={rt.status} {selected}>
		<Handle type="target" position={Position.Left} />

		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="tag-{id}">
			Tags stamped on every hit
		</label>
		<div class="mb-1.5 flex flex-wrap gap-1">
			{#each cfg.tags as tag (tag)}
				<span
					class="bg-primary/10 text-foreground inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[0.7rem]"
				>
					{tag}
					<button
						type="button"
						class="nodrag text-muted-foreground hover:text-destructive"
						aria-label="Remove tag {tag}"
						onclick={() => removeTag(tag)}
					>
						<X class="size-3" />
					</button>
				</span>
			{/each}
			{#if cfg.tags.length === 0}
				<span class="text-muted-foreground/70 text-[0.7rem]">no tags yet</span>
			{/if}
		</div>
		<input
			id="tag-{id}"
			class="{FIELD_CLASS} nodrag w-full"
			placeholder="type a tag, press Enter"
			bind:value={draft}
			onkeydown={(e) => {
				if (e.key === 'Enter') {
					e.preventDefault();
					addTag();
				}
			}}
		/>

		<!-- Run summary; hidden on error so the NodeShell banner stands alone. -->
		{#if rt.status !== 'error'}
			<div class="border-border mt-2 border-t pt-1.5 text-[0.7rem]">
				{#if rt.status === 'done'}
					<span class="text-muted-foreground">
						tagged <span class="text-foreground">{rt.count}</span> hits
					</span>
				{:else}
					<span class="text-muted-foreground/70">idle — wire results in, then Run</span>
				{/if}
			</div>
		{/if}

		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
