<script lang="ts">
	// The assist SETTINGS surface: which producers exist, whether each is real or mocked, what it
	// returns, and — when the canvas was opened from a task — whether that output can satisfy it.
	//
	// The owner asked for exactly this: "why is there no settings menu for which endpoints or runner
	// is configured.. and what they return". There was none. The registry was configuration you
	// could set and never see, which is how a service ends up pointed at a model whose output the
	// task refuses, with the mismatch only surfacing as a 409 after someone did the work.
	//
	// Read from the SERVICE, never from this pod's env. The `zoneConfig` query this replaces derived
	// its list by re-parsing MEDIA_ASSIST_BACKENDS locally, and its own comment conceded that could
	// drift from the service's. The service is the process that resolves and calls a backend.
	import { CircleAlert, CircleCheck, FlaskConical, Settings2 } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import { assistProducers, type ProducerListing } from '../remote/assist.remote';

	let { taskId = null }: { taskId?: string | null } = $props();

	let listing = $state<ProducerListing | null>(null);
	let error = $state<string | null>(null);
	let loaded = $state(false);

	// Loaded on OPEN, not on mount: every canvas would otherwise pay for a panel almost nobody
	// opens, and a settings read is not on the labeling path.
	async function load(): Promise<void> {
		if (loaded) return;
		loaded = true;
		const result = await assistProducers(taskId);
		if (result.ok) listing = result.data;
		else error = result.detail;
	}
</script>

<Popover.Root onOpenChange={(open) => open && load()}>
	<Popover.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				size="sm"
				title="Which model backends are configured, and what they return"
				data-testid="assist-registry-trigger"
			>
				<Settings2 class="size-3.5" />
			</Button>
		{/snippet}
	</Popover.Trigger>
	<!-- `p-3` is the consumer's job: the shared Content ships `p-0` so a panel can bleed a list to
	     its edges. Same pairing the lakehouse access Explorer uses. -->
	<Popover.Content class="w-80 p-3" data-testid="assist-registry">
		<p class="mb-2 text-sm font-medium">Model backends</p>

		{#if error}
			<!-- Named, not blank. "Nothing here" and "we could not ask" are different facts, and a
			     settings panel that cannot tell them apart is worse than no panel. -->
			<p class="text-destructive text-xs" data-testid="assist-registry-error">{error}</p>
		{:else if !listing}
			<p class="text-muted-foreground text-xs">reading the registry…</p>
		{:else}
			<ul class="space-y-2">
				{#each listing.producers as p (p.name)}
					<li class="flex items-start justify-between gap-2">
						<div class="min-w-0">
							<p class="truncate font-mono text-xs">{p.name}</p>
							<p class="text-muted-foreground text-xs">
								{#if p.returns.length}
									returns {p.returns.join(', ')}
								{:else}
									<!-- The registry is open by design, so the service meets names it knows
									     nothing about. Saying so beats assuming the common case. -->
									returns unknown
								{/if}
							</p>
						</div>
						<div class="flex shrink-0 items-center gap-1">
							{#if p.compatible === false}
								<Badge variant="destructive" title="This task will refuse what {p.name} returns">
									<CircleAlert class="size-3" /> off-contract
								</Badge>
							{:else if p.compatible === true}
								<Badge variant="outline" title="Its output satisfies this task">
									<CircleCheck class="size-3" /> fits task
								</Badge>
							{/if}
							{#if p.configured}
								<Badge variant="outline">live</Badge>
							{:else}
								<Badge
									variant="warning"
									title="No endpoint resolves — answers come from the in-repo mock"
								>
									<FlaskConical class="size-3" /> mock
								</Badge>
							{/if}
						</div>
					</li>
				{/each}
			</ul>
			<p class="text-muted-foreground mt-3 border-t pt-2 text-xs">
				{#if listing.default_configured}
					A default runner is set, so unregistered names reach it too.
				{:else}
					No default runner (<code>MEDIA_ASSIST_URL</code>) — unregistered names are mocked.
				{/if}
				Endpoints are redacted by the service.
			</p>
		{/if}
	</Popover.Content>
</Popover.Root>
