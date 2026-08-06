<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { Database, Play } from '@lucide/svelte';

	// QUERY ENGINE — SCAFFOLD (owner's ruling, 2026-08-06). No engine exists behind this yet.
	//
	// It ships visible rather than hidden for the reason R15 states: a zone surface missing from the
	// rail is a defect regardless of scaffold status, and a stub that NAMES what it will do is how the
	// shape gets reviewed before anything is built. The GPU page (/compute/gpu) is the same pattern.
	//
	// The one thing this deliberately does NOT do is pretend: the editor is inert, the run button is
	// disabled, and the panel below says which piece is missing rather than rendering fake rows. An
	// empty result table would be indistinguishable from a real query that matched nothing.
	let sql = $state('SELECT * FROM bronze$pages LIMIT 10');
</script>

<svelte:head><title>Query engine · compute · rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<Database class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Query engine</h1>
			<p class="text-muted-foreground text-sm">
				Run SQL against the lakehouse's governed tables and read the result here.
			</p>
		</div>
		<Badge variant="outline">Scaffold — no engine wired</Badge>
	</header>

	<Card class="flex flex-col gap-3 p-4">
		<label class="text-muted-foreground text-sm" for="q">Statement</label>
		<textarea
			id="q"
			bind:value={sql}
			rows="5"
			disabled
			class="border-border bg-muted/30 text-foreground w-full rounded-md border p-3 font-mono text-sm"
		></textarea>
		<div class="flex items-center gap-3">
			<button
				type="button"
				disabled
				class="bg-primary text-primary-foreground inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm opacity-50"
			>
				<Play class="size-4" /> Run
			</button>
			<span class="text-muted-foreground text-sm">Disabled until an engine answers.</span>
		</div>
	</Card>

	<Card class="flex flex-col gap-2 p-4">
		<h2 class="text-sm font-medium">What is missing</h2>
		<p class="text-muted-foreground text-sm">
			There is no query service in the fleet today. The catalog serves table
			<em>metadata</em>, and the explorer reads row batches as Arrow over its own
			<code class="text-foreground">/api/explorer</code> seam — neither accepts SQL. Wiring this
			means a real engine (a Lance/DataFusion door, or Ray Data) behind a gateway route, and it is a
			backend change, not a frontend one. This page exists so the surface can be argued about
			first.
		</p>
	</Card>
</div>
