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

	<Card class="flex flex-col gap-3 p-4">
		<h2 class="text-sm font-medium">The decision this page is waiting on</h2>
		<p class="text-muted-foreground text-sm">
			Two shapes, and they are not variants of each other — they put the engine in different places
			and imply different failure modes. Nothing here should be built until one is chosen.
		</p>

		<div class="border-border/60 flex flex-col gap-1 rounded-md border p-3">
			<h3 class="text-foreground text-sm font-medium">A · Embedded DataFusion</h3>
			<p class="text-muted-foreground text-sm">
				Register the Lance dataset directly and let DataFusion do parsing, logical planning,
				optimisation and streaming execution. Lance already ships the seam — pylance exposes
				<code class="text-foreground">FFILanceTableProvider</code>, so a dataset becomes a table the
				planner can push projections and filters into:
			</p>
			<pre class="bg-muted/40 mt-1 overflow-x-auto rounded p-2 text-xs"><code
					>{`from datafusion import SessionContext
from lance import FFILanceTableProvider

ctx = SessionContext()
ctx.register_table("t", FFILanceTableProvider(ds, with_row_id=True))
ctx.sql("SELECT * FROM t LIMIT 10")`}</code
				></pre>
			<p class="text-muted-foreground text-sm">
				The argument for it is that the expensive middle of a query engine — planner, optimiser,
				executor — is the part least likely to differentiate this platform, and it is already
				written. LanceDB, lance-graph, InfluxDB 3.0, Arroyo, ParadeDB and Spark (via Comet) all
				embed the same engine rather than building one. lance-graph is the sharpest case: it is a
				Cypher <em>frontend</em> lowered onto DataFusion logical plans, with no custom physical
				operators and no bespoke optimiser rules.
			</p>
		</div>

		<div class="border-border/60 flex flex-col gap-1 rounded-md border p-3">
			<h3 class="text-foreground text-sm font-medium">B · Ray + sqlglot, distributed</h3>
			<p class="text-muted-foreground text-sm">
				Parse and rewrite with sqlglot, fan the plan out over the Ray cluster this zone already
				runs. It buys horizontal scale over one query — and costs us the planner and optimiser
				that option A gets for free.
			</p>
		</div>

		<h3 class="text-foreground text-sm font-medium">Two questions that decide it</h3>
		<ul class="text-muted-foreground list-disc space-y-1 pl-5 text-sm">
			<li>
				<strong class="text-foreground">Where does the cache live?</strong> A hybrid
				memory/disk cache (foyer-style) beside the engine, or at the caller — a client-side cache
				makes the engine stateless and the cache a per-user concern; a server-side one makes
				repeat queries cheap for everyone and adds state to evict, invalidate and size.
			</li>
			<li>
				<strong class="text-foreground">Is “distributed” the right answer to the right
					question?</strong>
				The pressure we actually have is governance — FGA checks, auth, per-caller row and column
				scoping — not single-query throughput. Distributing execution does not help with that, and
				an embedded engine leaves the governance layer exactly where it already is. Choosing B for
				scale we do not yet measure would buy the wrong axis.
			</li>
		</ul>
		<p class="text-muted-foreground text-sm">
			Reference: <em>DataFusion and the Rise of Deconstructed Data Systems</em> — and the DataFusion
			SIGMOD paper it draws on.
		</p>
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
