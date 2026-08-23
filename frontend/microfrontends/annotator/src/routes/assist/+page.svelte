<script lang="ts">
	// AI assistance — the zone-level view of which model backends are configured.
	//
	// This one was going to be a stub and did not need to be. #36 already built the registry read
	// (`assistProducers`), and its `task_id` is OPTIONAL — passing none simply drops the per-task
	// compatibility column. So the honest page costs the same as the fake one, and a fake here would
	// have been the expensive kind of fake: someone reads "no backends" off a stub and goes looking
	// for a broken service.
	//
	// It is a PAGE and not only the canvas popover because the answer is zone-wide. The popover
	// (`AssistRegistry`) exists to answer "will this producer satisfy THIS task" while you are
	// standing on the canvas; it is trapped behind having a canvas open, which is the wrong place to
	// discover that everything in the estate is mocked.
	//
	// FAIL-HONEST is inherited from the query and matters more here: mock is the stack's default
	// state, so an unreachable service must not be allowed to render as "all live".
	import { onMount } from 'svelte';
	import { Bot, CircleAlert, FlaskConical } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { assistProducers, type ProducerListing } from '$lib/viewer/remote/assist.remote';

	// Read at zone level: no task, so no per-task compatibility verdict. `null` is the query's own
	// documented "no task" argument, not a stand-in for a missing one.
	let listing = $state<ProducerListing | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(true);

	// `onMount`, not `$effect` — the zone's own pattern (`ProjectsLanding`), and the correct one: a
	// fetch whose result is assigned into state is a one-shot on mount, not a reaction to a
	// dependency. An `$effect` here reads as "re-run when something changes" and there is nothing to
	// change; the autofixer flags exactly that. Unlike the canvas popover this IS the page's whole
	// purpose, so it loads eagerly rather than on open.
	onMount(async () => {
		const result = await assistProducers(null);
		if (result.ok) listing = result.data;
		else error = result.detail;
		loading = false;
	});
</script>

<svelte:head>
	<title>AI assistance · Annotate</title>
</svelte:head>

<div class="mx-auto flex max-w-3xl flex-col gap-6 p-8">
	<div class="flex flex-col gap-2">
		<div class="flex items-center gap-3">
			<Bot class="text-muted-foreground size-5 shrink-0" />
			<h1 class="text-xl font-semibold">AI assistance</h1>
		</div>
		<p class="text-muted-foreground text-sm">
			The model backends this zone can call to pre-fill annotations, and what each returns. Read
			from the annotation service, which is the process that resolves and calls them — not from this
			pod's own environment.
		</p>
	</div>

	{#if error}
		<!-- Named, not blank. "No producers" and "we could not ask" are different facts. -->
		<div
			class="border-destructive/40 bg-destructive/5 rounded-md border p-4"
			data-testid="assist-error"
		>
			<p class="text-destructive flex items-center gap-2 text-sm font-medium">
				<CircleAlert class="size-4 shrink-0" /> Could not read the assist registry
			</p>
			<p class="text-muted-foreground mt-1 text-xs">{error}</p>
			<p class="text-muted-foreground mt-2 text-xs">
				Until it answers, assume assistance is mocked — that is the stack's default state.
			</p>
		</div>
	{:else if loading}
		<p class="text-muted-foreground text-sm">reading the registry…</p>
	{:else if !listing || listing.producers.length === 0}
		<p class="text-muted-foreground text-sm" data-testid="assist-empty">
			No producers are registered. Assist requests fall through to the in-repo mock.
		</p>
	{:else}
		<ul class="divide-y rounded-md border" data-testid="assist-producers">
			{#each listing.producers as p (p.name)}
				<li class="flex items-start justify-between gap-4 p-4">
					<div class="min-w-0">
						<p class="truncate font-mono text-sm">{p.name}</p>
						<p class="text-muted-foreground mt-0.5 text-xs">
							{#if p.returns.length}
								returns {p.returns.join(', ')}
							{:else}
								<!-- The registry is open by design, so the service meets names it knows
								     nothing about. Saying so beats assuming the common case. -->
								returns unknown
							{/if}
						</p>
					</div>
					{#if p.configured}
						<Badge variant="outline" class="shrink-0">live</Badge>
					{:else}
						<Badge
							variant="warning"
							class="shrink-0"
							title="No endpoint resolves — answers come from the in-repo mock"
						>
							<FlaskConical class="size-3" /> mock
						</Badge>
					{/if}
				</li>
			{/each}
		</ul>

		<p class="text-muted-foreground text-xs">
			{#if listing.default_configured}
				A default runner is set, so unregistered names reach it too.
			{:else}
				No default runner (<code>MEDIA_ASSIST_URL</code>) — unregistered names are mocked.
			{/if}
			Endpoints are redacted by the service: presence is all a labeling surface needs, and an internal
			model-server URL is not an annotator's to have.
		</p>
	{/if}
</div>
