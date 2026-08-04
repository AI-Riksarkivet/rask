<script lang="ts">
	import { activeView, type DocTranscriptChunk } from '@rask/explorer-api';
	import { hitKey } from '$lib/utils';

	type Props = {
		chunks: DocTranscriptChunk[];
		duration: number;
		currentTime: number;
		activeKey: string | null;
		/** Index of the chunk the playhead is currently in — emphasised brighter +
		 *  taller than the opened-hit (activeKey) segment as the window slides. */
		currentChunkIdx: number;
		onSeek: (t: number) => void;
	};
	let { chunks, duration, currentTime, activeKey, currentChunkIdx, onSeek }: Props = $props();

	// Stable identity for a chunk = the descriptor's row key (matches the
	// activeKey the parent derives from the opened hit). Built per chunk.
	const key = (c: DocTranscriptChunk): string => hitKey(c);

	// Chunk time span + label read through the active view (raw row fields are
	// untyped passthrough) so this stays schema-agnostic.
	const view = activeView();

	// Playhead fraction in [0,1]; clamped so a stale currentTime can't overflow.
	const playFrac = $derived(duration > 0 ? Math.min(1, Math.max(0, currentTime / duration)) : 0);
</script>

{#if duration > 0 && chunks.length > 0}
	<!-- Counter row above the ribbon: which chunk of how many is playing. -->
	<div class="text-muted-foreground flex shrink-0 justify-end px-1 text-[0.7rem]">
		chunk {currentChunkIdx + 1} / {chunks.length}
	</div>
	<!-- overflow-x-hidden clips drifted-width segments at the track edges, but
       leaves overflow-y visible so the current segment's slight height bump
       (-top-0.5 / +0.25rem) and ring actually render instead of being clipped.
       my-0.5 reserves that vertical room so it doesn't collide with neighbours. -->
	<div
		class="bg-secondary/40 relative my-0.5 h-4 w-full shrink-0 overflow-x-hidden"
		role="group"
		aria-label="Chunk timeline"
	>
		{#each chunks as c, i (key(c))}
			{@const t = view.time(c)}
			{#if t}
				<!-- Clamp to [0,100] so drifted data (end>duration, end<start) can't
             overflow the track or render a negative-width segment. -->
				{@const left = Math.min(100, Math.max(0, (t.start / duration) * 100))}
				{@const width = Math.min(100 - left, Math.max(0, ((t.end - t.start) / duration) * 100))}
				{@const isCurrent = i === currentChunkIdx}
				{@const label = view.body(c)}
				<!-- Precedence: isCurrent (solid + ring + taller) > activeKey (opened hit)
             > muted. The current segment grows past the track via -top/extra
             height so it reads as "live" while the window slides. -->
				<button
					type="button"
					title={label}
					aria-label={label}
					onclick={() => onSeek(t.start)}
					class="border-background/70 hover:bg-primary/30 absolute cursor-pointer border-r transition-colors {isCurrent
						? 'bg-primary ring-primary -top-0.5 z-10 h-[calc(100%+0.25rem)] ring-1'
						: key(c) === activeKey
							? 'bg-primary/40 top-0 h-full'
							: 'bg-muted top-0 h-full'}"
					style="left: {left}%; width: {width}%;"
				></button>
			{/if}
		{/each}
		<!-- Playhead: a thin vertical line at currentTime/duration. pointer-events
         none so it never eats segment clicks. -->
		<div
			class="bg-primary pointer-events-none absolute top-0 z-20 h-full w-0.5"
			style="left: {playFrac * 100}%;"
		></div>
	</div>
{/if}
