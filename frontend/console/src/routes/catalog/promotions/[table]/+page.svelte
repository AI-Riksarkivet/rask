<script lang="ts">
	import { page } from '$app/state';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { GatedAction } from '@rask/ui/gated-action';
	import { formatAbsolute } from '@rask/ui/utils';
	import { reasonLabel } from '$lib/data/promotions';
	import { decidePromotion, getPromotion } from '$lib/data/remote/promotions.api';

	// One table's open hold, keyed by the table's id as it is named now.
	const table = $derived(page.params.table ?? '');
	const review = $derived(getPromotion({ table }));

	// The decision's own state, held here rather than derived from the read: a decision has three
	// outcomes a person must tell apart — in flight, refused, landed — and the read cannot say "your
	// click was refused", because the hold it reads is unchanged.
	let busy = $state(false);
	let outcome = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	async function decide(version: number, approve: boolean) {
		if (busy) return;
		busy = true;
		outcome = null;
		const result = await decidePromotion({ table, version, approve });
		busy = false;
		if (result.ok) {
			outcome = {
				tone: 'ok',
				text: approve
					? `Approved — published now names v${result.data.published_version ?? version}.`
					: `Rejected — the table is restored to v${result.data.restored_version ?? result.data.published_version ?? '?'}, and v${version} was not published.`,
			};
			return;
		}
		// 403 is the one a validator will actually hit, and it is not an outage: the decision is gated on
		// `can_promote`, above the ordinary publish. Naming the relation turns a dead end into a request.
		outcome = {
			tone: 'fail',
			text:
				result.status === 403
					? 'Refused: deciding a promotion needs `can_promote` on this table.'
					: result.status === 409
						? 'This hold was decided already, or published moved past it — reload to see where the table stands.'
						: `Could not record the decision (${result.status || 'offline'}). ${result.detail}`.trim(),
		};
	}
</script>

<svelte:head><title>Held promotion · rask</title></svelte:head>

<div class="flex flex-col gap-4 p-4">
	<div>
		<h1 class="text-lg font-semibold">Held promotion</h1>
		<p class="text-muted-foreground text-sm">
			The quality gate found this version unusual rather than broken, so it waits for a validator
			instead of being published. Approving moves <code>published</code> to it; rejecting restores
			the table to what <code>published</code> names and closes the hold for good.
		</p>
	</div>

	{#if review.error}
		<Card class="p-4"><p class="text-sm">Could not read this hold.</p></Card>
	{:else if review.current === undefined}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
	{:else if !review.current.ok}
		<Card class="p-4">
			<p class="text-sm">
				{#if review.current.status === 401}
					Sign in to view this hold.
				{:else}
					Could not read this hold ({review.current.status || 'offline'}).
				{/if}
			</p>
		</Card>
	{:else if review.current.data === null}
		<Card class="p-4">
			<p class="text-sm">
				No open hold on <code>{table}</code>. A hold is answerable only while it is open — once
				decided, or once it expires, it is gone.
			</p>
		</Card>
	{:else}
		{@const held = review.current.data}
		<Card class="flex flex-col gap-3 p-4">
			<div class="flex flex-wrap items-center gap-2">
				<Badge variant="warning">held</Badge>
				<code class="text-xs">{held.table}</code>
			</div>

			<dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
				<dt class="text-muted-foreground">Held version</dt>
				<dd>v{held.version}</dd>
				<dt class="text-muted-foreground">Published</dt>
				<dd>{held.published_version === null ? 'nothing yet' : `v${held.published_version}`}</dd>
				<dt class="text-muted-foreground">Expires</dt>
				<dd>{formatAbsolute(held.expires_at)}</dd>
				{#if held.requested_by}
					<dt class="text-muted-foreground">Requested by</dt>
					<dd><code class="text-xs">{held.requested_by}</code></dd>
				{/if}
			</dl>

			<div class="flex flex-col gap-1">
				<p class="text-muted-foreground text-xs uppercase tracking-wide">Why you are being asked</p>
				<ul class="flex flex-col gap-1 text-sm">
					{#each held.reasons as reason (reason)}
						<li>{reasonLabel(reason)}</li>
					{/each}
				</ul>
			</div>

			<div class="flex items-center gap-2">
				<!-- Shown-disabled-with-reason (#143), not hidden. The catalog re-checks regardless; this is
				     legibility, not enforcement. -->
				<GatedAction allowed={!busy} action="Approve" reason="A decision is already in flight.">
					<Button onclick={() => decide(held.version, true)}>
						{busy ? 'Recording…' : 'Approve'}
					</Button>
				</GatedAction>
				<GatedAction allowed={!busy} action="Reject" reason="A decision is already in flight.">
					<Button variant="destructive" onclick={() => decide(held.version, false)}>Reject</Button>
				</GatedAction>
			</div>
		</Card>
	{/if}

	<!-- Outside the hold's card: a landed decision closes the hold, the refreshed read then answers
	     "no open hold", and the outcome must still say what the click did. -->
	{#if outcome}
		<p role="status" class={outcome.tone === 'ok' ? 'text-sm' : 'text-destructive text-sm'}>
			{outcome.text}
		</p>
	{/if}
</div>
