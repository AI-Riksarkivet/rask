<script lang="ts">
	/** Model endpoint — the node this sandbox exists for. Picks a LIVE Ray Serve
	 *  app (discovered through the gateway's read-only /api/serve row) and POSTs
	 *  the upstream payload to it through the zone's /api/infer BFF. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { RefreshCw } from '@lucide/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { getServeApps } from '$lib/flows/remote/serve.remote';
	import { FIELD_CLASS } from './field';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);

	const appsQuery = getServeApps();
	const discovery = $derived(appsQuery.current);
	// The configured app may not be among the discovered ones (discovery down,
	// hand-typed slug) — surface it as an extra option so the bind stays honest.
	const options = $derived.by(() => {
		if (!discovery?.ok) return [];
		const apps = discovery.apps.map((a) => ({ value: a.app, label: `${a.name} · ${a.status}` }));
		if (cfg && !apps.some((a) => a.value === cfg.app))
			apps.push({ value: cfg.app, label: `${cfg.app} · custom` });
		return apps;
	});
</script>

{#if cfg && rt}
	<NodeShell {id} title="Model endpoint" status={rt.status} {selected}>
		<div class="mb-1 flex items-center justify-between">
			<label class="text-muted-foreground block text-[0.7rem]" for="app-{id}">Serve app</label>
			<button
				class="nodrag text-muted-foreground/60 hover:text-foreground rounded p-0.5"
				title="Refresh the live Serve app list"
				aria-label="Refresh Serve apps"
				onclick={() => void appsQuery.refresh().catch(() => {})}
			>
				<RefreshCw class="size-3" />
			</button>
		</div>
		{#if discovery?.ok && options.length}
			<select id="app-{id}" class="{FIELD_CLASS} w-full" bind:value={cfg.app}>
				{#each options as opt (opt.value)}
					<option value={opt.value}>{opt.label}</option>
				{/each}
			</select>
		{:else}
			<input
				id="app-{id}"
				class="{FIELD_CLASS} w-full"
				placeholder="htrflow"
				bind:value={cfg.app}
			/>
			<p class="text-muted-foreground mt-1 text-[0.7rem]">
				{#if discovery && !discovery.ok}
					Discovery offline ({discovery.detail}) — type the Serve app name.
				{:else}
					Looking for live Serve apps…
				{/if}
			</p>
		{/if}
		<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="body-{id}">Body</label>
		<select id="body-{id}" class="{FIELD_CLASS} w-full" bind:value={cfg.bodyMode}>
			<option value="upstream">Upstream payload (raw)</option>
			<option value="json">JSON template</option>
		</select>
		{#if cfg.bodyMode === 'json'}
			<textarea
				class="{FIELD_CLASS} nowheel mt-1 h-20 w-full resize-none font-mono"
				spellcheck="false"
				aria-label="JSON request body"
				bind:value={cfg.bodyTemplate}></textarea>
			<p class="text-muted-foreground mt-1 text-[0.65rem]">
				<code>{'{{input}}'}</code> becomes the upstream text.
			</p>
		{/if}
		<label class="text-muted-foreground mt-2 mb-1 block text-[0.7rem]" for="name-{id}"
			>Request name (optional)</label
		>
		<input
			id="name-{id}"
			class="{FIELD_CLASS} w-full"
			placeholder="page"
			bind:value={cfg.requestName}
		/>
		{#if rt.ms !== null}
			<p class="text-muted-foreground mt-2 text-[0.7rem]">Last call: {rt.ms} ms</p>
		{/if}
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
