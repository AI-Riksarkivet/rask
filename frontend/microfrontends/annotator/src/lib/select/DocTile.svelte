<script lang="ts">
	// One document card in the selection grid — thumbnail (cheap: the viewer's cached
	// /api/thumbnail blob), descriptor-driven title/metadata, duration badge. Dumb +
	// controlled, mirroring the media zone's gallery tile.
	import type { Document } from '@rask/explorer-api';
	import type { DatasetView } from '@rask/explorer-api/descriptor';
	import { cn } from '@rask/ui/utils';

	let { view, doc, onclick }: { view: DatasetView; doc: Document; onclick?: () => void } = $props();

	const title = $derived(view.title(doc));
	const duration = $derived(view.duration(doc));
	// Declared metadata minus any row that just repeats the title, as one compact line.
	const metaLine = $derived(
		view
			.metadata(doc)
			.filter((m) => m.value !== title)
			.map((m) => m.value)
			.join('  ·  '),
	);

	function fmtTime(s: number): string {
		const m = Math.floor(s / 60);
		return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	}
</script>

<button
	type="button"
	{onclick}
	data-testid="doc-tile"
	class={cn(
	// Card geometry straight off @rask/ui's Card (border-border / bg-card / rounded-lg /
	// shadow-sm), plus the lift + ring this tile needs as an interactive element.
	'group border-border bg-card text-card-foreground flex flex-col overflow-hidden rounded-lg border text-left shadow-sm transition-all',
	'hover:border-primary focus-visible:ring-ring/50 hover:-translate-y-0.5 hover:shadow-md focus-visible:ring-3 focus-visible:outline-none',
)}
>
	<div class="bg-muted relative aspect-video w-full overflow-hidden">
		<img
			src={view.thumbnailUrl(doc)}
			alt=""
			loading="lazy"
			class="h-full w-full object-cover transition-transform group-hover:scale-105"
			onerror={(e) => ((e.currentTarget as HTMLImageElement).style.visibility = 'hidden')}
		/>
		{#if duration != null}
			<!-- The duration pill sits ON the thumbnail, so it keeps a fixed dark scrim rather than a
			     theme token — a light-mode surface would vanish against a bright frame. -->
			<span
				class="absolute right-1.5 bottom-1.5 rounded-md bg-black/70 px-1.5 py-0.5 font-mono text-xs text-white backdrop-blur-sm"
			>
				{fmtTime(duration)}
			</span>
		{/if}
	</div>
	<div class="flex flex-1 flex-col gap-1 p-3">
		<div class="line-clamp-2 text-sm leading-snug font-medium">{title}</div>
		{#if metaLine}
			<div class="text-muted-foreground truncate font-mono text-xs" title={metaLine}>
				{metaLine}
			</div>
		{/if}
	</div>
</button>
