<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-ops>` — the ADMIN/OPERATIONS panel: the lineage DLQ (outbox) depth beside the
	 * control-plane change feed, mirroring the zone's own /admin/dlq and /admin/events surfaces —
	 * the same depth/oldest-age numbers, the same warn tone when the outbox is non-empty, the same
	 * action · object · actor · when columns — spelled in the utility classes elements.css compiles
	 * into this bundle (the host page's Tailwind build cannot generate them). The mount stamp + poll
	 * counter stay as the no-remount witness; rows dispatch the rask:select contract event instead of
	 * opening the drawer / navigating (a panel has no sheet and no router).
	 *
	 * BOTH reads are root-absolute and answer from any zone's page with the session riding:
	 *   · the DLQ through the shared lineage `client` → /lakehouse/api/admin/dlq  (GET-only BFF proxy)
	 *   · control events by plain fetch → /lakehouse/capi/v1/events?since=N       (GET-only BFF proxy)
	 *
	 * SCOPE, stated rather than implied: JetStream STREAM LAG is deliberately NOT here. The zone's
	 * /admin/streams reads the NATS monitor through `fetchJetstreamOverview` — a remote function that
	 * holds NATS_MONITOR_API server-side and is the only gate on an unauthenticated in-cluster
	 * monitor. There is no root-absolute route to it (the old /api/jetstream BFF route was folded
	 * INTO that remote function), and an element may not import '.remote'. Stream lag lands here the
	 * day a `GET /lakehouse/api/jetstream` (or a gateway `/api/jetstream`) exists again — not before,
	 * and never as a tile that renders a number it cannot read.
	 *
	 * The control feed keeps its OWN cursor across ticks, exactly as admin.remote.ts's generator
	 * does: re-polling `since=0` would re-deliver the whole ring buffer every tick, and the catalog
	 * audits every non-empty poll (`event_stream_opened`) — a panel left open would flood the #41
	 * audit trail. Cursor + `reset` + event_id dedup make each later tick an empty, unaudited one.
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { formatTimestamp } from '@rask/ui/utils';
	import type { DlqBacklog, DlqEvent } from '@rask/api/lineage';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';
	import { client } from './store';

	/** The catalog's CatalogControlEvent, structurally — mirrored (not imported) so this bundle does
	 *  not drag the zone's valibot schema module in for a shape it only reads. */
	type ControlEvent = {
		event_id: string;
		occurred_at: string;
		action: string;
		object_type: string;
		object_id: string;
		actor: string | null;
	};

	let { pollms = 15000 }: { pollms?: number } = $props();

	// ── the outbox (lineage /admin/dlq) ───────────────────────────────────────────────────────────
	const dlqPoll = new ElementPoll<DlqBacklog>(async () => {
		const res = await client.fetchDlq(50);
		if (!res.ok)
			throw new Error(res.status === 401 ? 'session expired or no access' : `HTTP ${res.status}`);
		return res.data;
	});
	$effect(() => dlqPoll.start(pollms));

	// ── the control-plane feed (catalog /v1/events), cursor-accumulated ───────────────────────────
	const WINDOW = 100;
	let cursor = 0;
	const seen = new Set<string>();
	// NOT named `window` (the generator's name) — that identifier is the global here, and shadowing it
	// inside a custom element's scope is how a bundle acquires a ghost.
	const recent: ControlEvent[] = [];

	const ctlPoll = new ElementPoll<ControlEvent[]>(async (f) => {
		const res = await f(`/lakehouse/capi/v1/events?since=${cursor}`);
		if (res.status === 401) throw new Error('session expired');
		if (res.status === 403) throw new Error('estate-admin only — the feed maps the whole estate');
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const page = (await res.json()) as {
			events?: ControlEvent[];
			cursor?: number;
			reset?: boolean;
		};
		cursor = page.cursor ?? cursor;
		if (page.reset === true) {
			recent.length = 0;
			seen.clear();
		}
		for (const ev of page.events ?? []) {
			if (seen.has(ev.event_id)) continue;
			seen.add(ev.event_id);
			recent.push(ev);
		}
		if (recent.length > WINDOW) {
			for (const dropped of recent.splice(0, recent.length - WINDOW)) seen.delete(dropped.event_id);
		}
		return recent.slice().reverse(); // newest first; a fresh copy so $state sees the change
	});
	$effect(() => ctlPoll.start(pollms));

	const backlog = $derived(dlqPoll.data);
	const atRisk = $derived(dlqPoll.data?.events ?? []);
	const changes = $derived(ctlPoll.data ?? []);

	// Mirrored from lib/admin/DlqPanel.svelte — the same age must read the same here.
	function age(seconds: number): string {
		if (seconds < 60) return `${Math.round(seconds)}s`;
		if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
		if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
		return `${Math.round(seconds / 86400)}d`;
	}

	// Mirrored from ControlEventsFeed.svelte — the action reads as words, not as a wire constant.
	const label = (a: string) => a.replaceAll('_', ' ');

	function selectDlq(node: HTMLElement, e: DlqEvent) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-ops',
					kind: 'dlq-event',
					id: e.run_id,
					label: e.job ?? e.run_id,
				} satisfies SelectDetail,
			}),
		);
	}

	function selectControl(node: HTMLElement, e: ControlEvent) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-ops',
					kind: 'control-event',
					id: e.event_id,
					label: `${label(e.action)} ${e.object_id}`,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">
		mounted {dlqPoll.mountedAt} · poll #{dlqPoll.polls}
	</p>

	<div class="flex flex-col gap-4 text-sm">
		<!-- Summary strip: the outbox saturation signal, then the change rate. -->
		<section class="grid gap-3 sm:grid-cols-3">
			<Card class="relative overflow-hidden p-4">
				<div
					class="absolute inset-x-0 top-0 h-0.5 {(backlog?.depth ?? 0) > 0
						? 'bg-warning'
						: 'bg-success'}"
				></div>
				<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
					DLQ depth
				</div>
				<div
					class="mt-1 font-mono text-2xl tabular-nums {(backlog?.depth ?? 0) > 0
						? 'text-warning'
						: ''}"
				>
					{backlog ? backlog.depth.toLocaleString() : '—'}
				</div>
				<div class="text-muted-foreground text-xs">staged, not yet confirmed delivered</div>
			</Card>

			<Card class="relative overflow-hidden p-4">
				<div class="absolute inset-x-0 top-0 h-0.5 bg-amber-500"></div>
				<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
					Oldest at-risk
				</div>
				<div class="mt-1 font-mono text-2xl tabular-nums">
					{backlog && backlog.depth > 0 ? age(backlog.oldest_age_seconds) : '—'}
				</div>
				<div class="text-muted-foreground text-xs">the relay drains on a timer</div>
			</Card>

			<Card class="relative overflow-hidden p-4">
				<div class="absolute inset-x-0 top-0 h-0.5 bg-sky-500"></div>
				<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
					Control events
				</div>
				<div class="mt-1 font-mono text-2xl tabular-nums">{changes.length}</div>
				<div class="text-muted-foreground text-xs">governance changes in the window</div>
			</Card>
		</section>

		<!-- The at-risk set (mirrors /admin/dlq's table, minus the drawer + replay: a panel has no
		     sheet, and replay is a WRITE — the zone's own page owns it). -->
		<Card class="overflow-hidden">
			<div
				class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
			>
				At-risk events ({atRisk.length})
			</div>
			{#if dlqPoll.error !== null}
				<p class="text-destructive px-4 py-3 text-sm">DLQ unavailable: {dlqPoll.error}</p>
			{:else if dlqPoll.data === null}
				<p class="text-muted-foreground px-4 py-3 text-sm">Reading the outbox…</p>
			{:else if atRisk.length === 0}
				<p class="text-muted-foreground px-4 py-3 text-sm">
					The outbox is empty — every committed write's lineage has been delivered.
				</p>
			{:else}
				<div class="max-h-full overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-muted/50 sticky top-0 z-10 text-left">
							<tr class="border-b">
								<th class="text-muted-foreground px-3 py-2 font-medium">run</th>
								<th class="text-muted-foreground w-32 px-3 py-2 font-medium">type</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">job</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">outputs</th>
							</tr>
						</thead>
						<tbody>
							{#each atRisk as e (e.run_id)}
								<tr
									class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
									onclick={(ev) => selectDlq(ev.currentTarget, e)}
								>
									<td class="text-muted-foreground px-3 py-2 align-middle font-mono whitespace-nowrap"
										>{e.run_id}</td
									>
									<td class="px-3 py-2 align-middle whitespace-nowrap">
										{#if e.parseable}
											<span class="font-mono">{e.event_type ?? '—'}</span>
										{:else}
											<Badge variant="destructive" class="px-1.5 py-px text-[10px]">poison</Badge>
										{/if}
									</td>
									<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">{e.job ?? '—'}</td>
									<td class="px-3 py-2 align-middle font-mono whitespace-nowrap"
										>{(e.outputs ?? []).join(', ') || '—'}</td
									>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</Card>

		<!-- Control-plane activity (mirrors /admin/events' feed, in the elements' table dress). -->
		<Card class="overflow-hidden">
			<div
				class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
			>
				Control-plane activity ({changes.length})
			</div>
			{#if ctlPoll.error !== null}
				<p class="text-destructive px-4 py-3 text-sm">Control feed unavailable: {ctlPoll.error}</p>
			{:else if ctlPoll.data === null}
				<p class="text-muted-foreground px-4 py-3 text-sm">Connecting to the control feed…</p>
			{:else if changes.length === 0}
				<p class="text-muted-foreground px-4 py-3 text-sm">No recent changes.</p>
			{:else}
				<div class="max-h-full overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-muted/50 sticky top-0 z-10 text-left">
							<tr class="border-b">
								<th class="text-muted-foreground px-3 py-2 font-medium">action</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">object</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">actor</th>
								<th class="text-muted-foreground w-28 px-3 py-2 font-medium">when</th>
							</tr>
						</thead>
						<tbody>
							{#each changes as e (e.event_id)}
								{@const when = formatTimestamp(e.occurred_at)}
								<tr
									class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
									onclick={(ev) => selectControl(ev.currentTarget, e)}
								>
									<td class="px-3 py-2 align-middle whitespace-nowrap">{label(e.action)}</td>
									<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">
										{#if e.object_type}
											<Badge
												variant="secondary"
												class="bg-accent/20 text-foreground mr-1.5 px-2 py-px text-[11px]"
												>{e.object_type}</Badge
											>
										{/if}{e.object_id}
									</td>
									<td class="text-muted-foreground px-3 py-2 align-middle font-mono whitespace-nowrap"
										>{e.actor ?? 'system'}</td
									>
									<td class="px-3 py-2 align-middle whitespace-nowrap">
										<span class="text-muted-foreground font-mono whitespace-nowrap" title={when.title}
											>{when.relative}</span
										>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</Card>

		<!-- Honest boundary, not a silent omission: see the scope note at the top of this file. -->
		<p class="text-muted-foreground text-[11px]">
			JetStream stream lag is not in this panel — the NATS monitor is reachable only server-side, from
			the zone's own /admin/streams page.
		</p>
	</div>
</div>
