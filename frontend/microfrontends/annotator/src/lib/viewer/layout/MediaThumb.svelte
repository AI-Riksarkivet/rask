<script lang="ts">
	// ONE thumbnail, with an honest fallback.
	//
	// Reported: "why dont all items have thumbnails? if audio it has thumbnails, if its a vieo it
	// will have thumbanils. If does not have thumbnails for some reason u use the media type default
	// thumbnails like icon for audio,img or video… gallerias and preview should asume their alys
	// thumbnails column."
	//
	// The ruling: a gallery may ASSUME a thumbnail exists. So the fallback is never a hole and never
	// a sentence — it is the media type's own glyph, which reads as "an audio item" rather than as
	// "something went wrong". The surfaces this replaces rendered the words "no preview", which at
	// tile size is indistinguishable from an error message.
	//
	// A BROKEN image is treated as an absent one. The previous code hid the `<img>` on error, leaving
	// a blank rectangle — the exact failure the icon exists to avoid, reached by a different route.
	import { FileAudio, FileVideo, Image as ImageIcon, FileText } from '@lucide/svelte';

	let {
		src = null,
		kind = 'image',
		alt = '',
		ratio = 'aspect-[4/3]',
		fit = 'cover',
	}: {
		/** The frame URL, or null when the item has none. */
		src?: string | null;
		/** The item's declared media kind — chooses the fallback glyph. */
		kind?: string;
		alt?: string;
		/** Tailwind sizing classes — the filmstrip wants square, the grid 4:3, quick view a max
		 *  height. Passed rather than chosen here, because how big a preview is belongs to the
		 *  surface showing it, not to the thing being shown. */
		ratio?: string;
		/** `cover` crops to fill (an index tile), `contain` shows the whole frame (quick view, where
		 *  you are READING the item and a crop would hide the part you need). */
		fit?: 'cover' | 'contain';
	} = $props();

	// `failed` is state, not derived: it is set by the image's own error event, which is a fact about
	// the network rather than about `src`. It resets when `src` changes because the component is
	// keyed on the item that owns it.
	let failed = $state(false);

	const Glyph = $derived(
		kind === 'audio' ? FileAudio : kind === 'video' ? FileVideo : kind === 'text' ? FileText : ImageIcon,
	);
</script>

{#if src && !failed}
	<img
		{src}
		{alt}
		loading="lazy"
		class="bg-muted {ratio} w-full {fit === 'contain' ? 'object-contain' : 'object-cover'}"
		onerror={() => (failed = true)}
		data-testid="media-thumb-img"
	/>
{:else}
	<!-- Labelled for assistive tech, glyph for sight. `kind` is in the title so a person who cannot
	     tell the glyphs apart at 24px still gets the answer. -->
	<div
		class="bg-muted text-muted-foreground flex {ratio} w-full items-center justify-center"
		title={`${kind} — no preview available`}
		role="img"
		aria-label={`${kind}, no preview available`}
		data-testid="media-thumb-fallback"
		data-kind={kind}
	>
		<Glyph class="size-6 opacity-60" />
	</div>
{/if}
