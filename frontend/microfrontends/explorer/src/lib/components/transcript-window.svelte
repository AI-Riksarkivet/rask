<script lang="ts">
	import { activeView, type DocTranscriptChunk } from '@rask/explorer-api';
	import { fmtTime, hitKey } from '$lib/utils';
	import { Play } from '@lucide/svelte';
	import TranscriptHighlighter from './transcript-highlighter.svelte';

	type Props = {
		/** The sliding window of chunks around the playing one (≤3). */
		chunks: DocTranscriptChunk[];
		/** Absolute index (within the whole doc) of the playing chunk. */
		currentChunkIdx: number;
		/** Absolute index of `chunks[0]` (the slice `lo`) — lets us flag the
		 *  playing block without re-deriving the window here. */
		windowStartIdx: number;
		/** Live media element, forwarded verbatim to each TranscriptHighlighter. */
		media: HTMLMediaElement | null;
		query?: string;
		/**
		 * `panel` (default): in-card, muted context via theme tokens.
		 * `overlay`: on the fullscreen dark scrim — muted context dims with
		 * `opacity` + light text so it stays legible on black.
		 */
		variant?: 'panel' | 'overlay';
	};
	let {
		chunks,
		currentChunkIdx,
		windowStartIdx,
		media,
		query = '',
		variant = 'panel',
	}: Props = $props();

	// Per-block container classes, split by variant + current/context. Built as
	// small lookups so the markup stays a single readable expression.
	const blockClass = (isCurrent: boolean): string => {
		const base = 'border-l-2 px-3 py-1';
		if (variant === 'overlay') {
			return isCurrent
				? `${base} border-primary bg-white/5 text-white`
				: `${base} border-transparent text-white/80 opacity-50`;
		}
		return isCurrent
			? `${base} border-primary bg-primary/5`
			: `${base} border-transparent text-muted-foreground opacity-60`;
	};
</script>

<div class={variant === 'overlay' ? 'text-left' : ''}>
	{#each chunks as c, j (hitKey(c))}
		{@const isCurrent = windowStartIdx + j === currentChunkIdx}
		{@const start = activeView().time(c)?.start ?? null}
		<div class={blockClass(isCurrent)}>
			<div class="text-muted-foreground flex items-center gap-1.5 py-0.5 font-mono text-xs">
				{#if start != null}<span>{fmtTime(start)}</span>{/if}
				{#if isCurrent}
					<span class="text-primary inline-flex items-center gap-0.5 font-sans font-medium">
						<Play class="size-3 fill-current" />playing
					</span>
				{/if}
			</div>
			<TranscriptHighlighter alignments={c.alignments ?? []} {media} {query} chrome={false} />
		</div>
	{/each}
</div>
