<script lang="ts">
	// The DLQ's row drawer: one at-risk event's staged fields and payload, a jump to each output
	// table's page, and a replay when the event parses.
	import { resolve } from '$app/paths';
	import { Button } from '@riksarkivet/ui/button';
	import * as Sheet from '@riksarkivet/ui/sheet';
	import { ExternalLink, RotateCcw } from '@lucide/svelte';
	import type { DlqEvent } from '#lib/api/lineage/index.ts';

	let {
		event,
		busy,
		onclose,
		onreplay,
	}: {
		/** The event shown; `null` keeps the drawer closed. */
		event: DlqEvent | null;
		/** A replay is in flight somewhere on the panel. */
		busy: boolean;
		onclose: () => void;
		onreplay: (event: DlqEvent) => void;
	} = $props();
</script>

<Sheet.Root
	open={event !== null}
	onOpenChange={(o) => {
		if (!o) onclose();
	}}
>
	<Sheet.Content side="right">
		{#if event}
			<Sheet.Header>
				<Sheet.Title>Staged event {event.run_id}</Sheet.Title>
				<Sheet.Description>
					{event.parseable
						? 'A committed write whose lineage is not yet confirmed delivered — replay re-ingests it now instead of waiting for the relay tick.'
						: 'A poison object: unparseable, so it cannot be replayed — the relay drops it on its next pass.'}
				</Sheet.Description>
			</Sheet.Header>
			<div class="flex flex-col gap-3.5 overflow-y-auto px-4 pb-4">
				<dl class="record">
					<dt>run</dt>
					<dd class="mono">{event.run_id}</dd>
					<dt>type</dt>
					<dd class={['mono', { 'text-destructive': !event.parseable }]}>
						{event.parseable ? (event.event_type ?? '—') : 'poison'}
					</dd>
					<dt>event time</dt>
					<dd class="mono">{event.event_time ?? '—'}</dd>
					<dt>job</dt>
					<dd class="mono">{event.job ?? '—'}</dd>
					<dt>inputs</dt>
					<dd class="mono">{(event.inputs ?? []).join(', ') || '—'}</dd>
					<dt>outputs</dt>
					<dd class="mono">{(event.outputs ?? []).join(', ') || '—'}</dd>
				</dl>
				<div>
					<h4 class="text-muted-foreground mb-1.5 text-[11px] tracking-wider uppercase">
						Payload (parsed staged fields)
					</h4>
					<pre
						class="bg-muted border-border text-foreground m-0 overflow-x-auto border px-3 py-2.5 font-mono text-[11px] leading-normal"
						aria-label="Staged event payload"><code>{JSON.stringify(event, null, 2)}</code></pre>
				</div>
				<div class="flex flex-col items-start gap-2">
					{#if event.parseable}
						{@const shown = event}
						<Button variant="outline" size="sm" disabled={busy} onclick={() => onreplay(shown)}>
							<RotateCcw size={12} /> Replay this event
						</Button>
					{/if}
					{#each event.outputs ?? [] as out (out)}
						<!-- The output dataset's table page is in the catalog AREA of the console — soft nav. -->
						<Button
							variant="outline"
							size="sm"
							href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(out) })}
						>
							<ExternalLink size={12} />
							{out} ↗
						</Button>
					{/each}
				</div>
			</div>
		{/if}
	</Sheet.Content>
</Sheet.Root>

<style>
	.record {
		display: grid;
		grid-template-columns: 84px 1fr;
		gap: 6px 10px;
		margin: 0;
		font-size: 12px;
	}
	.record dt {
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: 10.5px;
	}
	.record dd {
		margin: 0;
		color: var(--foreground);
		word-break: break-all;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
