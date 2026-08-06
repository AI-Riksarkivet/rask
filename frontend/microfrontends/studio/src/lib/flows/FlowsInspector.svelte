<script lang="ts">
	/** The right-hand pane: everything about the SELECTED node that does not fit on its card —
	 *  its metadata (kind, id, label, config, wiring) and a full-size PREVIEW of what it produced.
	 *
	 *  The node cards stay deliberately small: a card is for wiring, and a 260-px box cannot show an
	 *  ALTO document or a page scan. So the card shows status and the inspector shows the payload —
	 *  which is also why the preview here is not a second implementation of InspectNode's: both read
	 *  the same `runtime.output.payload` off the store. */
	import { Copy, Download, MousePointerClick } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { graph, nodeLabel, STATUS_DOT } from '$lib/flows/graph.svelte';
	import { payloadSummary } from '$lib/flows/types';

	const id = $derived(graph.inspectedNodeId);
	const kind = $derived(id ? graph.kindOf(id) : null);
	const cfg = $derived(id ? graph.config[id] : undefined);
	const rt = $derived(id ? graph.runtime[id] : undefined);
	const payload = $derived(rt?.output?.payload ?? null);
	const stale = $derived(id ? (rt?.stale ?? false) || graph.isOutdated(id) : false);

	/** Which nodes feed this one, and which it feeds — the wiring is metadata too, and reading it
	 *  here beats tracing edges by eye on a dense canvas. */
	const upstream = $derived(
		id ? graph.edges.filter((e) => e.target === id).map((e) => e.source) : [],
	);
	const downstream = $derived(
		id ? graph.edges.filter((e) => e.source === id).map((e) => e.target) : [],
	);

	/** The config fields that actually belong to this kind — showing all of them would print five
	 *  irrelevant defaults per node (the flat NodeConfig is a POC shortcut, see types.ts). */
	const fields = $derived.by((): { label: string; value: string }[] => {
		if (!cfg || !kind) return [];
		switch (kind) {
			case 'image':
				return [{ label: 'File', value: cfg.fileName || '(none chosen)' }];
			case 'text':
				return [{ label: 'Text', value: cfg.text || '(empty)' }];
			case 'model':
				return [
					{ label: 'Serve app', value: cfg.app || '(unset)' },
					{ label: 'Request name', value: cfg.requestName || 'page' },
				];
			default:
				return [];
		}
	});

	// A blob URL for an image payload, owned by the <img> through an attachment so the URL is
	// revoked when the payload changes or the pane unmounts — no state variable to leak.
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

	/** Download the payload as a file — the sandbox's way out: an ALTO document you want to keep,
	 *  or a transcription to paste elsewhere. */
	function download(): void {
		if (!payload) return;
		const blob =
			payload.kind === 'text'
				? new Blob([payload.text], { type: payload.mime || 'text/plain' })
				: new Blob([payload.bytes], { type: payload.mime });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download =
			payload.kind === 'text' && payload.mime.includes('xml') ? 'output.xml' : 'output.txt';
		a.click();
		URL.revokeObjectURL(url);
	}
</script>

<div class="border-border bg-card flex h-full min-h-0 flex-col border-l">
	{#if !id || !kind || !cfg || !rt}
		<div
			class="text-muted-foreground flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-xs"
		>
			<MousePointerClick class="size-5" />
			<p>Click a node to inspect it — its config, its wiring, and whatever it produced.</p>
		</div>
	{:else}
		<!-- Header: what this node IS -->
		<div class="border-border flex shrink-0 items-center gap-2 border-b px-3 py-2">
			<span class="size-2 shrink-0 rounded-full {STATUS_DOT[rt.status]}"></span>
			<div class="min-w-0 flex-1">
				<div class="text-foreground truncate text-sm font-semibold">
					{cfg.label.trim() || nodeLabel(kind)}
				</div>
				<div class="text-muted-foreground truncate text-[0.7rem]">{kind} · {id}</div>
			</div>
			{#if stale}
				<span
					class="shrink-0 rounded-md bg-amber-500/15 px-1 text-[9px] tracking-wide text-amber-600 uppercase dark:text-amber-400"
					>stale</span
				>
			{/if}
		</div>

		<div class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 text-xs">
			{#if rt.error}
				<div
					class="border-destructive/30 bg-destructive/10 text-destructive rounded-md border px-2 py-1 text-[0.7rem] leading-snug break-words"
				>
					{rt.error}
				</div>
			{/if}

			<!-- Metadata -->
			<section class="flex flex-col gap-1">
				<h3 class="text-muted-foreground text-[0.65rem] font-medium tracking-wide uppercase">
					Metadata
				</h3>
				<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
					<dt class="text-muted-foreground">Status</dt>
					<dd class="text-foreground truncate">{rt.status}</dd>
					{#if rt.ms !== null}
						<dt class="text-muted-foreground">Last call</dt>
						<dd class="text-foreground">{rt.ms} ms</dd>
					{/if}
					<dt class="text-muted-foreground">Enabled</dt>
					<dd class="text-foreground">{cfg.enabled ? 'yes' : 'no (bypassed)'}</dd>
					{#each fields as f (f.label)}
						<dt class="text-muted-foreground">{f.label}</dt>
						<dd class="text-foreground truncate" title={f.value}>{f.value}</dd>
					{/each}
					<dt class="text-muted-foreground">Inputs</dt>
					<dd class="text-foreground truncate">{upstream.join(', ') || '—'}</dd>
					<dt class="text-muted-foreground">Outputs</dt>
					<dd class="text-foreground truncate">{downstream.join(', ') || '—'}</dd>
				</dl>
			</section>

			<!-- Label, editable here rather than on the card: renaming is an occasional act and the
			     card's chrome is already dense. -->
			<section class="flex flex-col gap-1">
				<label
					class="text-muted-foreground text-[0.65rem] font-medium tracking-wide uppercase"
					for="label-{id}"
				>
					Label
				</label>
				<input
					id="label-{id}"
					class="border-border bg-background text-foreground focus:border-primary rounded border px-2 py-1 text-xs outline-none"
					placeholder={nodeLabel(kind)}
					bind:value={cfg.label}
				/>
			</section>

			<!-- Preview -->
			<section class="flex min-h-0 flex-col gap-1">
				<div class="flex items-center justify-between gap-2">
					<h3 class="text-muted-foreground text-[0.65rem] font-medium tracking-wide uppercase">
						Output
					</h3>
					{#if payload}
						<div class="flex items-center gap-1">
							{#if payload.kind === 'text'}
								<Button size="sm" variant="ghost" onclick={copyText} title="Copy the output">
									<Copy class="size-3" />
								</Button>
							{/if}
							<Button size="sm" variant="ghost" onclick={download} title="Download the output">
								<Download class="size-3" />
							</Button>
						</div>
					{/if}
				</div>
				{#if payload}
					<p class="text-muted-foreground truncate text-[0.65rem]" title={payload.label}>
						{payload.label} — {payloadSummary(payload)}
					</p>
					{#if payload.kind === 'text'}
						<pre
							class="border-border bg-background max-h-[28rem] overflow-auto rounded-md border p-2 text-[0.7rem] leading-snug whitespace-pre-wrap">{payload.text ||
				'(empty)'}</pre>
					{:else if payload.mime.startsWith('image/')}
						<img
							alt={payload.label}
							class="border-border bg-muted max-h-[28rem] w-full rounded-md border object-contain"
							{@attach blobSrc(payload.bytes, payload.mime)}
						/>
					{:else}
						<p class="text-muted-foreground text-[0.7rem]">{payloadSummary(payload)}</p>
					{/if}
				{:else}
					<p class="text-muted-foreground text-[0.7rem]">
						Nothing yet — run the flow, or press ▶ on this node.
					</p>
				{/if}
			</section>
		</div>
	{/if}
</div>
