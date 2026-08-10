<script lang="ts">
	// A rail row whose SCREEN does not exist yet.
	//
	// The owner asked for five rows in the annotate rail and said three of them "can be dummies".
	// A dummy has one hard requirement: it must not be mistakable for the real thing. A page that
	// renders a plausible empty table is worse than no page — someone reads it as "the judge found
	// nothing" rather than "the judge does not exist", and that is a false statement about the
	// system that costs a debugging session to unwind.
	//
	// So it says so, in the heading, in a badge, and in the tab title. What it is NOT is an error:
	// this route resolves, is linked from the rail on purpose, and describes work that is planned.
	// The `Construction` icon rather than an alert glyph carries that difference.
	//
	// The estate's other "cannot open this" surface is `ZoneNavLeaf.unavailable`, which greys a rail
	// row out. That one is for a capability THE DATA lacks (this corpus has no knowledge graph) and
	// is decided per-descriptor at runtime. This one is for a screen the PRODUCT has not built, which
	// is a fact about the repo — so it is a route, not a disabled link, and it can explain itself at
	// length where a tooltip cannot.
	import { Construction } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';

	let {
		title,
		summary,
		intent,
	}: {
		/** The rail row's own label — the two must match, or the page reads as the wrong page. */
		title: string;
		/** One sentence: what this screen is for. */
		summary: string;
		/** The bullets that make it concrete — what it will show, and where that data comes from. */
		intent: string[];
	} = $props();
</script>

<svelte:head>
	<!-- The "not built" fact belongs in the tab title too: a pinned tab is the one place someone
	     reads a page name with none of the page around it. -->
	<title>{title} — not built yet · Annotate</title>
</svelte:head>

<div class="mx-auto flex max-w-2xl flex-col gap-6 p-8" data-testid="planned-area">
	<div class="flex flex-col gap-3">
		<div class="flex items-center gap-3">
			<Construction class="text-muted-foreground size-5 shrink-0" />
			<h1 class="text-xl font-semibold">{title}</h1>
			<Badge variant="outline" data-testid="planned-badge">Not built yet</Badge>
		</div>
		<p class="text-muted-foreground text-sm">{summary}</p>
	</div>

	<div class="flex flex-col gap-2">
		<p class="text-sm font-medium">What it will do</p>
		<ul class="text-muted-foreground flex list-disc flex-col gap-1.5 pl-5 text-sm">
			{#each intent as line (line)}
				<li>{line}</li>
			{/each}
		</ul>
	</div>

	<p class="text-muted-foreground border-t pt-4 text-xs">
		This route exists so the rail names every job this zone does, rather than only the ones already on
		screen. Nothing here reads or writes annotation data.
	</p>
</div>
