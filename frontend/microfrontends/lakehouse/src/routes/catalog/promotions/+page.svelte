<script lang="ts">
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Input } from '@rask/ui/input';

	// NO LIST, AND THAT IS THE BACKEND'S SHAPE RATHER THAN AN OMISSION HERE. The producer serves
	// exactly `GET /promotions/{instance_id}` and `POST /promotions/{instance_id}/decision` — there is
	// no index door to call, because a review lives inside a running Dapr workflow rather than in a
	// table something can scan. Rendering a fabricated or cached list would be worse than none: it
	// would go stale the moment a review is decided elsewhere, and a validator would act on a row that
	// no longer exists.
	//
	// So this page does the two honest things it can: take an id, and say where holds are actually
	// visible today.
	let instanceId = $state('');
	const target = $derived(instanceId.trim());

	function open(event: SubmitEvent) {
		event.preventDefault();
		if (target) void goto(`${base}/catalog/promotions/${encodeURIComponent(target)}`);
	}
</script>

<svelte:head><title>Promotions · lance</title></svelte:head>

<div class="flex flex-col gap-4 p-4">
	<div>
		<h1 class="text-lg font-semibold">Promotions</h1>
		<p class="text-muted-foreground text-sm">
			A promotion the quality gate found unusual waits for a validator rather than being dropped.
			Open one by its review id to approve or reject it.
		</p>
	</div>

	<Card class="flex flex-col gap-3 p-4">
		<form class="flex flex-wrap items-end gap-2" onsubmit={open}>
			<label class="flex flex-col gap-1 text-sm">
				<span class="text-muted-foreground">Review id</span>
				<Input bind:value={instanceId} placeholder="promotion-…" class="w-80" />
			</label>
			<Button type="submit" disabled={!target}>Open</Button>
		</form>
		<p class="text-muted-foreground text-xs">
			The id is <code>promotion-&lt;run token&gt;</code>. There is no list endpoint — a review lives
			inside its running workflow, not in a table, so this page cannot enumerate them.
		</p>
	</Card>

	<Card class="flex flex-col gap-2 p-4">
		<h2 class="text-sm font-semibold">Where a hold shows up</h2>
		<p class="text-muted-foreground text-sm">
			A held promotion emits its own FAIL run, whose message reads
			<em>“quality gate HELD the promotion into …”</em>. Both surfaces below list it, newest first.
		</p>
		<div class="flex flex-wrap gap-2">
			<Button variant="outline" href="{base}/lineage/runs">Runs board</Button>
			<Button variant="outline" href="{base}/lineage">Lineage graph</Button>
		</div>
	</Card>
</div>
