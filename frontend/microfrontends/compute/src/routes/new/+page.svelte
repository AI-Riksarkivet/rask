<script lang="ts">
	import { base } from '$app/paths';
	import { startIngest, type IngestAccepted } from '@rask/api';
	import { getIngestSources } from '$lib/remote/ingest.remote';
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

	const registry = getIngestSources();
	const sources = $derived(registry.current ?? []);

	// `chosen` is what the USER picked; `kind` falls back to the first registered kind until they
	// pick one. Two variables rather than one seeded in an $effect: seeding state from an effect
	// re-runs whenever the query refreshes, which would silently reset a half-filled form back to
	// the first kind. The select writes through the function-pair binding below.
	let chosen = $state<string | null>(null);
	const kind = $derived(chosen ?? sources[0]?.kind ?? '');

	let project = $state('demo');
	let dataset = $state('');
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
	const canIngest = $derived(
		Boolean(selected) && validProject && validDataset && missing.length === 0 && !busy,
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

	async function ingest() {
		busy = true;
		error = null;
		result = null;
		try {
			result = await startIngest({
				kind,
				project,
				dataset,
				options: buildOptions(),
				// A retry after a 503 converges onto the same run instead of double-firing it. Keyed on
				// the whole request, so changing any field is a different run rather than a dedupe onto
				// the previous one.
				idempotencyKey: `ui-${kind}-${project}-${dataset}-${JSON.stringify(buildOptions())}`,
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

			<div class="grid grid-cols-2 gap-3">
				<label class="block space-y-1">
					<span class="text-sm font-medium">Project</span>
					<input
						class="bg-background w-full rounded border px-3 py-2 font-mono"
						placeholder="demo"
						bind:value={project}
						disabled={busy}
					/>
					{#if project && !validProject}
						<span class="text-destructive text-xs">Letters, digits, . - and _ only.</span>
					{/if}
				</label>

				<label class="block space-y-1">
					<span class="text-sm font-medium">Dataset</span>
					<input
						class="bg-background w-full rounded border px-3 py-2 font-mono"
						placeholder="pages"
						bind:value={dataset}
						disabled={busy}
					/>
					{#if dataset && !validDataset}
						<span class="text-destructive text-xs">Letters, digits, . - and _ only.</span>
					{/if}
				</label>
			</div>

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
