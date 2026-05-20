<script lang="ts">
	import { Badge } from '$lib/components/ui/badge';
	import { ExternalLink, Search } from 'lucide-svelte';
	import type { CatalogHit } from '$lib/api';

	interface Props {
		hit: CatalogHit;
		/** Optional FTS query — used to highlight matched tokens in titles +
		 *  description. Pass empty string for browse mode (no highlights). */
		highlightQuery?: string;
	}
	let { hit, highlightQuery = '' }: Props = $props();

	// Cached-or-better volumes open in our viewer; the rest route out to
	// Riksarkivet's bildvisning. Listed-but-not-cached has no images on disk
	// so opening it locally would 404 — keep those external too.
	const localHref = $derived(
		hit.cached ? `/viewer/${encodeURIComponent(hit.bild_id)}` : null,
	);
	const externalHref = $derived(hit.bildvisning_url || null);

	const accentClass = $derived(
		hit.transcribed
			? 'border-emerald-500/40'
			: hit.cached
				? 'border-blue-500/30'
				: hit.listed
					? 'border-amber-500/30'
					: '',
	);

	function fmtDate(h: CatalogHit): string {
		if (h.date_text) return h.date_text;
		if (h.date_start && h.date_end) {
			return h.date_start === h.date_end
				? String(h.date_start)
				: `${h.date_start}–${h.date_end}`;
		}
		return '';
	}

	/** Split text by case-insensitive token matches against `query`.
	 *  Returns the original text in one segment when query is empty. */
	function splitForHighlight(text: string, query: string): { text: string; match: boolean }[] {
		const tokens = query.toLowerCase().split(/\s+/).filter(Boolean);
		if (tokens.length === 0) return [{ text, match: false }];
		const escaped = tokens.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
		const re = new RegExp(`(${escaped.join('|')})`, 'gi');
		const out: { text: string; match: boolean }[] = [];
		let last = 0;
		for (const m of text.matchAll(re)) {
			const i = m.index!;
			if (i > last) out.push({ text: text.slice(last, i), match: false });
			out.push({ text: m[0], match: true });
			last = i + m[0].length;
		}
		if (last < text.length) out.push({ text: text.slice(last), match: false });
		return out;
	}
</script>

<a
	href={localHref || externalHref || '#'}
	target={localHref ? undefined : '_blank'}
	rel={localHref ? undefined : 'noopener'}
	class="flex flex-col gap-2 rounded-md border bg-card p-3 text-left transition hover:border-primary/50 hover:bg-accent/30 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none {accentClass}"
>
	<div class="flex items-start gap-3">
		<div class="min-w-0 flex-1">
			<div class="flex flex-wrap items-center gap-2 text-base font-medium">
				{#each splitForHighlight(hit.fonds_title || '(untitled fonds)', highlightQuery) as seg}
					{#if seg.match}<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
							>{seg.text}</mark
						>{:else}{seg.text}{/if}
				{/each}
				{#if hit.series_title}
					<span class="text-muted-foreground">›</span>
					<span>
						{#each splitForHighlight(hit.series_title, highlightQuery) as seg}
							{#if seg.match}<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
									>{seg.text}</mark
								>{:else}{seg.text}{/if}
						{/each}
					</span>
				{/if}
				{#if hit.volume_title}
					<span class="text-muted-foreground">›</span>
					<span class="font-mono text-sm">
						{#each splitForHighlight(hit.volume_title, highlightQuery) as seg}
							{#if seg.match}<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
									>{seg.text}</mark
								>{:else}{seg.text}{/if}
						{/each}
					</span>
				{/if}
			</div>
			{#if hit.description}
				<div class="mt-1 line-clamp-2 text-sm text-muted-foreground">
					{#each splitForHighlight(hit.description, highlightQuery) as seg}
						{#if seg.match}<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
								>{seg.text}</mark
							>{:else}{seg.text}{/if}
					{/each}
				</div>
			{/if}
			<div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
				<Badge variant="secondary">{hit.archive_code}</Badge>
				{#if fmtDate(hit)}<span>{fmtDate(hit)}</span>{/if}
				<span class="font-mono">{hit.reference_code}</span>
				{#if hit.transcribed}
					<Badge class="bg-emerald-500/20 text-emerald-700 dark:text-emerald-300">Transcribed</Badge>
				{:else if hit.cached}
					<Badge class="bg-blue-500/20 text-blue-700 dark:text-blue-300">Cached</Badge>
				{:else if hit.listed}
					<Badge class="bg-amber-500/20 text-amber-700 dark:text-amber-300">Listed</Badge>
				{/if}
			</div>
		</div>
		{#if localHref}
			<Search class="mt-1 h-4 w-4 shrink-0 text-emerald-600" />
		{:else if externalHref}
			<ExternalLink class="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />
		{/if}
	</div>
</a>
