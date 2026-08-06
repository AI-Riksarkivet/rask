<script lang="ts">
	/** Inspect sink — renders whatever payload reaches it (the sandbox's output
	 *  pane): text/XML in a scrollable pre, image bytes as a preview. Forwards
	 *  the payload unchanged, so it can also sit mid-chain as a probe. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { Copy } from '@lucide/svelte';
	import { graph } from '$lib/flows/graph.svelte';
	import { payloadSummary } from '$lib/flows/types';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const cfg = $derived(graph.config[id]);
	const rt = $derived(graph.runtime[id]);
	const payload = $derived(rt?.output?.payload ?? null);

	/** Image-bytes payloads render as a blob-URL preview; the attachment owns
	 *  the URL's whole lifecycle on the <img> itself — it re-runs when `bytes`
	 *  changes and revokes the previous URL in its cleanup, with no state var. */
	function blobSrc(bytes: ArrayBuffer, mime: string) {
		return (el: HTMLImageElement) => {
			const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
			el.src = url;
			return () => URL.revokeObjectURL(url);
		};
	}

	function copyText(): void {
		if (payload?.kind === 'text') void navigator.clipboard.writeText(payload.text);
	}
</script>

{#if cfg && rt}
	<NodeShell {id} title="Inspect" status={rt.status} {selected} width="w-80">
		{#if payload}
			<div class="mb-1 flex items-center justify-between gap-2">
				<span class="text-muted-foreground truncate text-[0.7rem]" title={payload.label}>
					{payload.label} — {payloadSummary(payload)}
				</span>
				{#if payload.kind === 'text'}
					<button
						class="nodrag text-muted-foreground/60 hover:text-foreground shrink-0 rounded p-0.5"
						title="Copy the output text"
						aria-label="Copy output"
						onclick={copyText}
					>
						<Copy class="size-3" />
					</button>
				{/if}
			</div>
			{#if payload.kind === 'text'}
				<pre
					class="nodrag nowheel border-border bg-background max-h-48 overflow-auto rounded-md border p-2 text-[0.7rem] leading-snug whitespace-pre-wrap">{payload.text ||
			'(empty)'}</pre>
			{:else if payload.mime.startsWith('image/')}
				<img
					alt={payload.label}
					class="border-border bg-muted max-h-48 w-full rounded-md border object-contain"
					{@attach blobSrc(payload.bytes, payload.mime)}
				/>
			{:else}
				<p class="text-muted-foreground text-[0.7rem]">{payloadSummary(payload)}</p>
			{/if}
		{:else}
			<p class="text-muted-foreground text-[0.7rem]">
				Run the flow — whatever reaches this node renders here.
			</p>
		{/if}
		<Handle type="target" position={Position.Left} />
		<Handle type="source" position={Position.Right} />
	</NodeShell>
{/if}
