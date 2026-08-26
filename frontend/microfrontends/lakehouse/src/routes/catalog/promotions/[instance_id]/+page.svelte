<script lang="ts">
	import { page } from '$app/state';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { GatedAction } from '@rask/ui/gated-action';
	import { reasonLabel } from '$lib/data/promotions';
	import { decidePromotion, getHeldPromotion } from '$lib/data/remote/promotions.remote';

	const instanceId = $derived(page.params.instance_id ?? '');
	const review = $derived(getHeldPromotion({ instanceId }));

	// The mutation's own state, held here rather than derived from the query: a decision is a single
	// act with three outcomes a person must be able to tell apart — in flight, refused, landed — and
	// the query cannot express "your click was refused" because the review it reads is unchanged.
	let busy = $state(false);
	let outcome = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	async function decide(approved: boolean) {
		if (busy) return;
		busy = true;
		outcome = null;
		const result = await decidePromotion({ instanceId, approved });
		busy = false;
		if (result.ok) {
			outcome = {
				tone: 'ok',
				text: approved
					? `Approved — the cascade resumed and published ${result.data.dataset ?? 'the destination'}.`
					: 'Rejected — the promotion stays held and nothing was published.',
			};
			return;
		}
		// 403 is the one a validator will actually hit, and it is not an outage: the decision rung is
		// `can_promote` on the destination namespace, above the ordinary publish's `can_update_tag`.
		// Naming the relation is what turns a dead end into a request they can make of someone.
		outcome = {
			tone: 'fail',
			text:
				result.status === 403
					? 'Refused: deciding a promotion needs `can_promote` on the destination namespace.'
					: result.status === 404
						? 'This review is no longer live — it was decided already, or its approval window closed.'
						: `Could not record the decision (${result.status}). ${result.detail ?? ''}`.trim(),
		};
	}
</script>

<svelte:head><title>Held promotion · rask</title></svelte:head>

<div class="flex flex-col gap-4 p-4">
	<div>
		<h1 class="text-lg font-semibold">Held promotion</h1>
		<p class="text-muted-foreground text-sm">
			A stage's quality gate found this promotion unusual rather than broken, so it is waiting for a
			validator instead of being dropped. Approving resumes the cascade; rejecting leaves it held.
		</p>
	</div>

	{#if review.error}
		<Card class="p-4">
			<p class="text-sm">Could not read this review.</p>
		</Card>
	{:else if review.loading}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
	{:else if review.current}
		{@const result = review.current}
		{#if !result.ok}
			<Card class="p-4">
				<p class="text-sm">
					{#if result.status === 404}
						No live review under <code>{instanceId}</code>. A promotion is answerable only while its
						workflow is waiting — once decided, or once the approval window closes, it is gone.
					{:else if result.status === 401}
						Sign in to view this review.
					{:else}
						Could not read this review ({result.status}).
					{/if}
				</p>
			</Card>
		{:else}
			{@const held = result.data}
			<Card class="flex flex-col gap-3 p-4">
				<div class="flex flex-wrap items-center gap-2">
					<Badge variant="warning">held</Badge>
					<code class="text-xs">{held.instance_id}</code>
				</div>

				<dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
					<dt class="text-muted-foreground">Project</dt>
					<dd>{held.project}</dd>
					<dt class="text-muted-foreground">From</dt>
					<dd><code class="text-xs">{held.from_dataset}</code></dd>
					<dt class="text-muted-foreground">To</dt>
					<dd><code class="text-xs">{held.to_dataset}</code></dd>
					{#if held.approval_hours}
						<dt class="text-muted-foreground">Window</dt>
						<dd>{held.approval_hours} hours from when it was held</dd>
					{/if}
				</dl>

				<div class="flex flex-col gap-1">
					<p class="text-muted-foreground text-xs uppercase tracking-wide">
						Why you are being asked
					</p>
					<ul class="flex flex-col gap-1 text-sm">
						{#each held.reasons as reason (reason)}
							<li>{reasonLabel(reason)}</li>
						{/each}
					</ul>
				</div>

				<div class="flex items-center gap-2">
					<!-- Shown-disabled-with-reason (#143), not hidden: a validator who lacks the rung must
					     see that the control exists and what it needs, so they can ask the person who can
					     grant it. The server re-checks regardless — this is legibility, not enforcement. -->
					<GatedAction allowed={!busy} action="Approve" reason="A decision is already in flight.">
						<Button onclick={() => decide(true)}>
							{busy ? 'Recording…' : 'Approve'}
						</Button>
					</GatedAction>
					<GatedAction allowed={!busy} action="Reject" reason="A decision is already in flight.">
						<Button variant="destructive" onclick={() => decide(false)}>Reject</Button>
					</GatedAction>
				</div>

				{#if outcome}
					<p class={outcome.tone === 'ok' ? 'text-sm' : 'text-destructive text-sm'}>
						{outcome.text}
					</p>
				{/if}
			</Card>
		{/if}
	{/if}
</div>
