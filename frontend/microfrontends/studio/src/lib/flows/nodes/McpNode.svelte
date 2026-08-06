<script lang="ts">
	/** MCP tool — call a Model Context Protocol tool as a flow step. SCAFFOLD, and it says so.
	 *
	 *  MCP servers in this repo are Claude Code's SESSION tooling (registered by
	 *  `make claude-bootstrap`), not a service the app can reach. Invoking one from a flow needs an
	 *  MCP client and a stdio/SSE transport running server-side — a real backend capability that does
	 *  not exist yet — so running this node raises with that reason instead of faking a result. The
	 *  shape is here because an agent-shaped flow (model decides, tool acts) is the direction, and a
	 *  named gap is worth more than an absent one. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
</script>

{#if cfg && rt}
	<NodeShell {id} title="MCP tool" status={rt.status} {selected}>
		<span
			class="mb-1 inline-block rounded-md bg-amber-500/15 px-1 text-[9px] tracking-wide text-amber-600 uppercase dark:text-amber-400"
			title="No server-side MCP client exists yet — running this node says so rather than faking a result"
		>
			scaffold
		</span>
		<label class="text-muted-foreground mb-1 block text-[0.7rem]" for="srv-{id}">Server</label>
		<input
			id="srv-{id}"
			class="{FIELD_CLASS} w-full"
			placeholder="ra-mcp"
			bind:value={cfg.mcpServer}
		/>
		<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="tool-{id}">Tool</label>
		<input
			id="tool-{id}"
			class="{FIELD_CLASS} w-full"
			placeholder="search_transcribed"
			bind:value={cfg.mcpTool}
		/>
		<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="args-{id}">
			Arguments (JSON)
		</label>
		<textarea
			id="args-{id}"
			class="{FIELD_CLASS} nowheel h-14 w-full resize-none font-mono"
			spellcheck="false"
			bind:value={cfg.mcpArgs}></textarea>
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
