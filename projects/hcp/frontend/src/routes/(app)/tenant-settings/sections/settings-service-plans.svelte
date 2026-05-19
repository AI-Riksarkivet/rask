<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import { get_service_plans, type ServicePlan } from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let plansData = $derived(get_service_plans({ tenant }));
	let plans = $derived((plansData?.current ?? []) as ServicePlan[]);
</script>

{#await plansData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Available Service Plans</Card.Title>
			<Card.Description>
				Storage tiers available for namespaces in this tenant. Service plans are defined at the
				system level and cannot be modified here.
			</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if plans.length > 0}
				<div class="space-y-3">
					{#each plans as plan (plan.name)}
						<div class="flex items-start justify-between rounded-md border px-4 py-3">
							<div class="space-y-1">
								<div class="flex items-center gap-2">
									<span class="font-medium">{plan.name}</span>
									<Badge variant="secondary">Plan</Badge>
								</div>
								{#if plan.description}
									<p class="text-sm text-muted-foreground">{plan.description}</p>
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{:else}
				<p class="text-sm text-muted-foreground">No service plans available for this tenant.</p>
			{/if}
		</Card.Content>
	</Card.Root>
{/await}
