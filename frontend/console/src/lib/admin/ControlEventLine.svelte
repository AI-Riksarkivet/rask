<script lang="ts">
	// One control event, as every list of them prints it: the action, the object by its name, for a
	// grant the relation and the subject it names, the actor, and the full local date and time with
	// its zone.
	import { formatZoned } from '@rask/ui/utils';
	import { Subject } from '#lib/access/identity/index.ts';
	import { grantDetail, objectLabel, type ControlEvent } from './control.ts';

	let { event }: { event: ControlEvent } = $props();

	const grant = $derived(grantDetail(event));
</script>

<li class="event">
	<span class="action">{event.action.replaceAll('_', ' ')}</span>
	<span class="obj mono" title={event.object_id}>{objectLabel(event)}</span>
	<span class="grant">
		{#if grant}
			<span class="mono">{grant.relation}</span>
			{event.action === 'grant_revoked' ? 'from' : 'to'}
			<Subject value={grant.subject} />
			{#if grant.condition}<span class="cond">(time-boxed: {grant.condition})</span>{/if}
		{/if}
	</span>
	<span class="by">by <Subject value={event.actor} /></span>
	<time class="ts mono" datetime={event.occurred_at}>{formatZoned(event.occurred_at)}</time>
</li>

<style>
	.event {
		display: grid;
		grid-template-columns: 9rem minmax(8rem, 1fr) minmax(10rem, 1.4fr) minmax(10rem, 1.2fr) auto;
		gap: 12px;
		align-items: baseline;
		padding: 6px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
		font-size: 12px;
	}
	@media (max-width: 860px) {
		.event {
			grid-template-columns: 1fr;
			gap: 2px;
		}
	}
	.action {
		color: var(--foreground);
		font-weight: 500;
	}
	.obj,
	.grant,
	.by,
	.cond {
		color: var(--muted-foreground);
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.ts {
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
