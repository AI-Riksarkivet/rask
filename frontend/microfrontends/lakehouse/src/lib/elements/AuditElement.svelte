<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/** `<rask-lakehouse-audit>` — the governance audit trail, VISUALLY IDENTICAL to the zone's
	 *  /admin/audit table: the same Card-bounded rows, the same five columns in the same order, the
	 *  same relative-stamp-with-tooltip `when` cell and the same ALLOW/DENY tone — the utility class
	 *  strings ARE the page's, compiled into this bundle by elements.css since the host page's
	 *  Tailwind build cannot generate them. Reads the zone's own /lakehouse/api/audit BFF
	 *  (root-absolute, session riding); the shape mirrors AuditViewer's structural type. The mount
	 *  stamp + poll counter stay as the no-remount witness; rows dispatch the rask:select contract
	 *  event instead of opening the drawer (a panel has no sheet). */
	import { formatTimestamp } from '@rask/ui/utils';
	import { Card } from '@rask/ui/card';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';

	type AuditEvent = {
		timestamp: string;
		action: string;
		outcome: string;
		subject: string;
		resource: string;
	};

	let { pollms = 30000, filtertext = '' }: { pollms?: number; filtertext?: string } = $props();
	const poll = new ElementPoll<AuditEvent[]>(async (f) => {
		const res = await f('/lakehouse/api/audit?limit=100');
		if (res.status === 401) throw new Error('session expired or no access');
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const body = (await res.json()) as { events?: AuditEvent[] };
		return body.events ?? [];
	});
	$effect(() => poll.start(pollms));

	const events = $derived(poll.data ?? []);
	/** The cross-filter (wave 3): the compositor pushes the active selection's label down as a
	 *  property; rows narrow by substring over their serialized form — generic on purpose, every
	 *  list element filters the same way. */
	const shownevents = $derived(
		filtertext.trim() === ''
			? events
			: events.filter((r) => JSON.stringify(r).toLowerCase().includes(filtertext.toLowerCase())),
	);

	// Mirrored from lib/admin/AuditViewer.svelte — the same outcomes must wear the same colour here.
	// (The zone spells the tone as a scoped class over the legacy palette; `--ok`/`--fail` bridge to
	// `--success`/`--destructive`, so these are the same two colours as utilities.)
	function tone(o: string): string {
		if (o === 'DENY' || o === 'FAILURE') return 'text-destructive';
		if (o === 'ALLOW' || o === 'SUCCESS') return 'text-success';
		return '';
	}

	// GreptimeDB's `timestamp` column arrives as a raw NANOSECOND epoch integer, which `new Date()`
	// cannot parse — the shared @rask/ui formatter reads the unit off the magnitude and returns both
	// forms: the row shows the distance and hangs the exact stamp in its tooltip, exactly as the page.

	function select(node: HTMLElement, ev: AuditEvent) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-audit',
					kind: 'audit-event',
					id: `${ev.timestamp}:${ev.action}`,
					label: `${ev.action} ${ev.resource}`,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if filtertext.trim() !== '' && shownevents.length !== events.length}
		<p class="text-muted-foreground mb-1 text-[11px]">
			filtered: {shownevents.length}/{events.length} match “{filtertext}”
		</p>
	{/if}
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Audit trail unavailable: {poll.error}</p>
	{:else if events.length === 0}
		<p class="text-muted-foreground text-sm">No audit events in the window.</p>
	{:else}
		<Card class="overflow-hidden">
			<div class="max-h-full overflow-auto">
				<table class="w-full border-collapse text-xs">
					<thead class="bg-muted/50 sticky top-0 z-10 text-left">
						<tr class="border-b">
							<th class="text-muted-foreground px-3 py-2 font-medium">when</th>
							<th class="text-muted-foreground px-3 py-2 font-medium">action</th>
							<th class="text-muted-foreground w-28 px-3 py-2 font-medium">outcome</th>
							<th class="text-muted-foreground px-3 py-2 font-medium">subject</th>
							<th class="text-muted-foreground px-3 py-2 font-medium">resource</th>
						</tr>
					</thead>
					<tbody>
						{#each shownevents as ev, i (i)}
							{@const when = formatTimestamp(ev.timestamp)}
							<tr
								class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
								onclick={(e) => select(e.currentTarget, ev)}
							>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									<span class="text-muted-foreground font-mono whitespace-nowrap" title={when.title}
										>{when.relative}</span
									>
								</td>
								<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">
									<span class="font-mono">{ev.action || '—'}</span>
								</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									<span class="font-mono {tone(ev.outcome)}">{ev.outcome || '—'}</span>
								</td>
								<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">
									<span class="font-mono">{ev.subject || '—'}</span>
								</td>
								<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">
									<span class="font-mono">{ev.resource || '—'}</span>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</Card>
	{/if}
</div>
