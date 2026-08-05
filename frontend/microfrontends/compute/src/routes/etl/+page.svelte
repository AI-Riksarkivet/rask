<script lang="ts">
	import { base } from '$app/paths';
	import type { IngestAccepted } from '@rask/api';
	// `startIngest` comes from the REMOTE module, not from `@rask/api` directly. Calling the client
	// helper from here posted with cookies but no bearer, so the door saw the gateway's own identity
	// and refused it ("'gateway' is a public front door"). The remote command runs on the zone server,
	// where the session bearer exists. Found by pressing the button, not by a test.
	import { getIngestSources, startIngest } from '$lib/remote/ingest.remote';
	import { Card } from '@rask/ui/card';
	import { Button } from '@rask/ui/button';
	import { CloudDownload } from '@lucide/svelte';

	const ID_RE = /^[A-Za-z0-9._-]+$/;

	// The ingest form — the plane's head (`POST /api/ingest/ingests`, open_ingest.md P1).
	//
	// The door takes {kind, project, dataset, options} and resolves the adapter from a registry, and
	// this page now takes the same shape. It used to call `ingestIIIFVolume()` with kind 'iiif',
	// project 'default' and dataset 'pages' baked in — directly beneath a comment saying the door is
	// source-agnostic. That is the weld I1 removed from the backend, re-formed one layer out, and it
	// is why `S3PrefixSource` was reachable by curl but not by anyone using the product.
	//
	// So the kinds and their fields are READ from `GET /api/ingest/sources` rather than restated
	// here. Adding a source touches an adapter and a registry entry; this page picks it up because it
	// asks. Gate A9.
	//
	// Pure action page: one mutation on submit, no reads beyond the registry, so no remote queries.

	// THE PROJECT IS CONTEXT, NOT A FIELD. This page had a free-text "Project" input defaulting to
	// 'demo', while the navbar's project picker was already setting the active project on the #103
	// cookie that `zoneLayoutLoad` reads. Two sources of truth for one value, and the form's won — so
	// a user standing in project A could type project B and post rows there, with nothing in the UI
	// disagreeing. Reading it from layout data makes the write scope equal the browsing scope, which
	// is also what the door's authorization check targets (`authorize_ingest(..., body.project)`).
	let { data }: { data: { activeProject: string } } = $props();
	const project = $derived(data.activeProject);

	const registry = getIngestSources();
	const sources = $derived(registry.current ?? []);

	// `chosen` is what the USER picked; `kind` falls back to the first registered kind until they
	// pick one. Two variables rather than one seeded in an $effect: seeding state from an effect
	// re-runs whenever the query refreshes, which would silently reset a half-filled form back to
	// the first kind. The select writes through the function-pair binding below.
	let chosen = $state<string | null>(null);
	const kind = $derived(chosen ?? sources[0]?.kind ?? '');

	let dataset = $state('');

	// ── how this run partitions its writes (POST body `sizing`) ──────────────────────────
	//
	// Four numbers the API takes per run, EMPTY meaning "use the deployment default". They are here
	// because the right value is a property of the SOURCE, not of the cluster: a rate-limited IIIF
	// endpoint and an S3 bucket in the same datacentre want different fetch concurrency, and ten
	// thousand 20 MB page images want a different fragment shape from a million 40 KB records. Until
	// now the only lever was an operator restarting the deployment with different env, which meant
	// one setting for every source the estate would ever ingest.
	//
	// Strings, not numbers: '' is the only honest spelling of "I did not set this", and a numeric
	// input bound to a number coerces an emptied field to 0 — which the door would read as a
	// deliberate zero and refuse.
	let sizing = $state<Record<string, string>>({});
	let showAdvanced = $state(false);
	// Keyed by `${kind}.${option}` so switching kinds and switching back does not silently discard
	// what was typed — two kinds may both have a `prefix` that means different things.
	let values = $state<Record<string, string>>({});
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<IngestAccepted | null>(null);

	const selected = $derived(sources.find((s) => s.kind === kind) ?? null);
	const fieldKey = (name: string) => `${kind}.${name}`;

	const missing = $derived(
		(selected?.options ?? [])
			.filter((o) => o.required && !(values[fieldKey(o.name)] ?? '').trim())
			.map((o) => o.label),
	);
	const validProject = $derived(ID_RE.test(project));
	const validDataset = $derived(ID_RE.test(dataset));

	// The one sizing value the CLIENT can refuse locally, because getting it wrong is not a validation
	// error — it is a HANG. A batch is held unacked while it fills, JetStream stops delivering at
	// `max_ack_pending` (2048), and the drain then waits forever for units it will never be sent. The
	// door refuses it too (400, naming the ceiling); saying so here means the user finds out while
	// typing rather than after dispatch. 1M is the number someone reads off Lance's own guidance,
	// which is exactly why it needs an answer at the point of entry.
	const ACK_CEILING = 2048;
	const SIZING_FIELDS = [
		{
			name: 'fragment_rows',
			label: 'Rows per fragment',
			placeholder: '1024',
			help: `Rows accumulated before one Lance fragment is written. Must stay under ${ACK_CEILING} — the queue's unacked ceiling — so services/maintenance compacts toward Lance's larger target afterwards.`,
		},
		{
			name: 'fragment_bytes',
			label: 'Bytes per fragment',
			placeholder: '268435456',
			help: '…or this many payload bytes, whichever comes first. This is the limit that actually fires on media: 1024 twenty-megabyte pages would be a 20 GB fragment.',
		},
		{
			name: 'fetch_batch',
			label: 'Queue fetch batch',
			placeholder: '16',
			help: 'Units pulled from the queue per round-trip.',
		},
		{
			name: 'fetch_concurrency',
			label: 'Source concurrency',
			placeholder: '8',
			help: 'In-flight fetches against the SOURCE. A politeness ceiling for rate-limited endpoints, not a throughput dial.',
		},
	] as const;

	const badSizing = $derived(
		SIZING_FIELDS.filter((f) => {
			const raw = (sizing[f.name] ?? '').trim();
			if (raw === '') return false;
			const n = Number(raw);
			if (!Number.isInteger(n) || n < 1) return true;
			return f.name === 'fragment_rows' && n >= ACK_CEILING;
		}).map((f) => f.label),
	);

	/** Only fields the user filled in — an empty string is "unset", not "set to empty".
	 *
	 *  Sending `{prefix: ''}` is not the same as omitting it: the adapter's `or ""` fallbacks make
	 *  them coincide today, but an option whose default is not the empty string would be silently
	 *  overridden by a field the user never touched. */
	function buildOptions(): Record<string, unknown> {
		const out: Record<string, unknown> = {};
		for (const option of selected?.options ?? []) {
			const raw = (values[fieldKey(option.name)] ?? '').trim();
			if (!raw) continue;
			out[option.name] = option.numeric ? Number(raw) : raw;
		}
		return out;
	}

	const badNumbers = $derived(
		(selected?.options ?? [])
			.filter((o) => {
				const raw = (values[fieldKey(o.name)] ?? '').trim();
				return o.numeric && raw !== '' && !(Number.isInteger(Number(raw)) && Number(raw) >= 1);
			})
			.map((o) => o.label),
	);

	const canIngest = $derived(
		Boolean(selected) &&
			validProject &&
			validDataset &&
			missing.length === 0 &&
			badNumbers.length === 0 &&
			badSizing.length === 0 &&
			!busy,
	);

	/** The sizing overrides the user actually set — the same "empty means unset" rule as the options.
	 *
	 *  Sending `{fragment_rows: null}` and omitting the key are the same to the door (every field is
	 *  optional there), but an empty object is what makes an ordinary run byte-identical to what it
	 *  posted before this block existed. */
	function buildSizing(): Record<string, number> {
		const out: Record<string, number> = {};
		for (const field of SIZING_FIELDS) {
			const raw = (sizing[field.name] ?? '').trim();
			if (raw) out[field.name] = Number(raw);
		}
		return out;
	}

	async function ingest() {
		busy = true;
		error = null;
		result = null;
		try {
			const options = buildOptions();
			const sizingBody = buildSizing();
			result = await startIngest({
				kind,
				project,
				dataset,
				options,
				sizing: sizingBody,
				// A retry after a 503 converges onto the same run instead of double-firing it. Keyed on
				// the whole request — SIZING INCLUDED, because two runs of the same source at different
				// fragment shapes are different runs and must not dedupe onto each other.
				idempotencyKey: `ui-${kind}-${project}-${dataset}-${JSON.stringify(options)}-${JSON.stringify(sizingBody)}`,
			});
		} catch (e: unknown) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head>
	<title>Ingest — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<Card class="m-4 max-w-2xl space-y-4 p-6">
		<h1 class="text-lg font-semibold">Ingest a source</h1>
		<p class="text-muted-foreground text-sm">
			Accepts a run that harvests the source into a bronze dataset; the cascade then runs event-driven.
			The run starts in the background — this form returns as soon as it is dispatched, and progress
			lands in the pipeline-runs feed on the landing page.
		</p>

		{#if registry.error}
			<!-- The registry is the page's content, not a decoration on it: with no kinds there is
			     nothing to submit, and a form rendered anyway would post a kind the door refuses. -->
			<p class="text-destructive text-sm">
				Could not read the source registry: {registry.error.message}
			</p>
		{:else if registry.loading}
			<p class="text-muted-foreground text-sm">Reading the source registry…</p>
		{:else if sources.length === 0}
			<p class="text-destructive text-sm">No source kinds are registered on this deployment.</p>
		{:else}
			<label class="block space-y-1">
				<span class="text-sm font-medium">Source kind</span>
				<select
					class="bg-background w-full rounded border px-3 py-2"
					data-testid="source-kind"
					bind:value={() => kind, (next) => (chosen = next)}
					disabled={busy}
				>
					{#each sources as source (source.kind)}
						<option value={source.kind}>{source.label}</option>
					{/each}
				</select>
				{#if selected?.description}
					<span class="text-muted-foreground block text-xs">{selected.description}</span>
				{/if}
			</label>

			<label class="block space-y-1">
				<span class="text-sm font-medium">Table</span>
				<input
					class="bg-background w-full rounded border px-3 py-2 font-mono"
					data-testid="ingest-table"
					placeholder="pages"
					bind:value={dataset}
					disabled={busy}
				/>
				{#if dataset && !validDataset}
					<span class="text-destructive text-xs">Letters, digits, . - and _ only.</span>
				{:else}
					<!-- Naming the resolved catalog id, because "table" alone hides where the rows land.
					     The plane addresses by {project, dataset} and the catalog's ensure() turns that
					     into a NAMESPACE named for the project holding a table of this name — so this is
					     the identifier the lakehouse will show, not a decoration. -->
					<span class="text-muted-foreground block text-xs">
						Lands as <span class="font-mono">{project || '<project>'}${dataset || '<table>'}</span>
						in bronze.
					</span>
				{/if}
			</label>

			<!-- THE PROJECT, shown and not editable. It comes from the navbar's project picker (the
			     #103 active-project cookie), which is the same scope the door authorizes the write
			     against. A second, typeable copy here is how a user in project A posts into B. -->
			{#if !validProject}
				<p class="text-destructive text-sm" data-testid="no-active-project">
					No active project — pick one in the navbar before starting a run.
				</p>
			{/if}

			{#each selected?.options ?? [] as option (option.name)}
				<label class="block space-y-1">
					<span class="text-sm font-medium">
						{option.label}
						{#if !option.required}
							<span class="text-muted-foreground font-normal">(optional)</span>
						{/if}
					</span>
					<input
						class="bg-background w-full rounded border px-3 py-2 font-mono"
						data-testid={`source-option-${option.name}`}
						placeholder={option.placeholder ?? ''}
						inputmode={option.numeric ? 'numeric' : undefined}
						bind:value={values[fieldKey(option.name)]}
						disabled={busy}
					/>
					{#if option.help}
						<span class="text-muted-foreground block text-xs">{option.help}</span>
					{/if}
				</label>
			{/each}

			{#if badNumbers.length > 0}
				<p class="text-destructive text-sm">
					A whole number ≥ 1, or leave empty: {badNumbers.join(', ')}.
				</p>
			{/if}

			<!-- ADVANCED: how this run partitions its writes. Collapsed because every field is
			     optional and the defaults are right for most sources — but reachable, because the
			     right value is a property of the SOURCE and only the person choosing the source
			     knows it. Before this, the only way to change any of it was an operator restarting
			     the deployment with different env, which is one setting for every source forever. -->
			<details class="rounded border" bind:open={showAdvanced}>
				<summary class="cursor-pointer px-3 py-2 text-sm font-medium" data-testid="sizing-advanced">
					Partitioning &amp; throughput
					<span class="text-muted-foreground font-normal">(optional — deployment defaults apply)</span>
				</summary>
				<div class="space-y-3 border-t px-3 py-3">
					<p class="text-muted-foreground text-xs">
						The run writes Lance fragments as it goes; these decide how big each one is and how
						hard the source is pushed. Leave a field empty to use the deployment's value.
					</p>
					{#each SIZING_FIELDS as field (field.name)}
						<label class="block space-y-1">
							<span class="text-sm font-medium">{field.label}</span>
							<input
								class="bg-background w-full rounded border px-3 py-2 font-mono"
								data-testid={`sizing-${field.name}`}
								inputmode="numeric"
								placeholder={field.placeholder}
								bind:value={sizing[field.name]}
								disabled={busy}
							/>
							<span class="text-muted-foreground block text-xs">{field.help}</span>
						</label>
					{/each}
				</div>
			</details>

			{#if badSizing.length > 0}
				<p class="text-destructive text-sm" data-testid="sizing-error">
					{#if badSizing.includes('Rows per fragment') && Number(sizing.fragment_rows ?? 0) >= ACK_CEILING}
						Rows per fragment must stay under {ACK_CEILING}. Above that the drain holds more
						unacked messages than the queue will deliver and the run hangs rather than failing —
						compaction is what grows fragments toward Lance's larger target, not this field.
					{:else}
						A whole number ≥ 1, or leave empty: {badSizing.join(', ')}.
					{/if}
				</p>
			{/if}

			<Button onclick={ingest} disabled={!canIngest}>
				<CloudDownload class="h-4 w-4" />
				{busy ? 'Dispatching…' : 'Start ingest'}
			</Button>

			{#if missing.length > 0}
				<p class="text-muted-foreground text-xs">Still needed: {missing.join(', ')}.</p>
			{/if}
		{/if}

		{#if error}<p class="text-destructive text-sm">{error}</p>{/if}
		{#if result}
			<div class="space-y-1 rounded border border-emerald-600 p-3 text-sm">
				<!-- ACCEPTED, not "harvested". The old head declared 202 and then blocked through the
				     whole harvest, so this panel could honestly say the pages had landed. The run is now
				     genuinely asynchronous: nothing has been fetched yet when this renders, and claiming
				     otherwise would be the same declared-but-absent semantics the plane exists to fix. -->
				<p>
					Accepted <strong class="font-mono">{project}/{dataset}</strong> — run
					<span class="font-mono">{result.run_id}</span>
					{#if result.deduplicated}
						<!-- Worth saying out loud: the user pressed the button and NO new work started.
						     Silence here reads as a no-op bug rather than idempotency working. -->
						<em>(already running — the same Idempotency-Key resolved to this run)</em>
					{/if}
				</p>
				<a class="underline" href={base}>Back to the runs feed</a>
			</div>
		{/if}
	</Card>
</main>
