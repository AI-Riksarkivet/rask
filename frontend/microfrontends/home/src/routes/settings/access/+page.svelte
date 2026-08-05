<script lang="ts">
	// `/settings/access` — ONE query-driven FGA explorer.
	//
	// An ESTATE surface, and that is why it is HERE (#105): the workbench reads the whole tuple store
	// across every project and takes none, so it belongs to estate settings rather than inside a
	// project-scoped zone. It served from `/lakehouse/governance/access` until this port; the shell's
	// Settings entry already lit on both while the move was pending.
	//
	// This replaced four tabs (Graph / Tuples / Check / Model) that shared no state, no URL and no
	// question. Each was a one-to-one wrapper around one catalog endpoint, so the view could only ask
	// what the API happened to expose — and the API exposed neither ListObjects, ListUsers nor Expand.
	// Switching tab threw away whatever you were looking at, which made the one workflow that matters
	// ("why can alice not see this table") a sequence of disconnected screens and a lot of retyping.
	//
	// Now: the canvas is always mounted, the query bar highlights it, the facet rail narrows what is
	// already on screen (no refetch), and the inspector sits beside it. The state lives in the URL, so
	// an access question is a link you can paste into an incident channel.
	import { ShieldCheck } from '@lucide/svelte';
	import Explorer from '$lib/access/Explorer.svelte';
</script>

<svelte:head><title>Access · lance</title></svelte:head>

<div class="mx-auto flex max-w-[1600px] flex-col gap-3 px-5 pb-6 pt-8">
	<header class="flex items-baseline gap-2.5">
		<ShieldCheck size={16} />
		<h1 class="text-xl font-semibold">Access</h1>
		<span class="text-xs text-muted-foreground">
			the live authorization graph — ask what a subject can reach, who holds a relation, or why one
			resolves
		</span>
	</header>

	<Explorer />
</div>
