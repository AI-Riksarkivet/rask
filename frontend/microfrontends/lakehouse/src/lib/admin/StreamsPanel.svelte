<script lang="ts">
	// `/streams` — read-only JetStream diagnostics over the estate's event fabric. The /api/jetstream BFF
	// estate-admin-gates (catalog can_observe_events), fetches the unauthenticated NATS monitor `/jsz` server-side, and
	// returns a trimmed typed overview — the monitor URL and raw payload never reach the browser. This panel
	// renders stream cards with per-consumer lag (pending / ack-pending / redelivered): redeliveries are the
	// wedge signal, backlog the pressure signal. On top of the raw state it diagnoses: EXPECTED consumers
	// that are absent (a dead subscription — invisible in /jsz itself, the worst historical failure mode),
	// stale consumers (no delivery in 10 min), and +N message deltas between manual refreshes. Strictly a
	// viewer — no mutation affordances by design.
	import { Layers, RefreshCw, ShieldAlert, TriangleAlert } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { page } from '$app/state';
	import { JetStreamOverviewSchema, type JetStreamOverview } from './jetstream';
	import { requestJSON } from '$lib/http';
	import StreamConsumers from './StreamConsumers.svelte';
	import { jetstreamCursor } from '$lib/live/feeds.remote';
	import { liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let overview = $state<JetStreamOverview | null>(null);
	// The PREVIOUS successful read's message counts (totals + per-stream), so the panel can show "+N since
	// the last change" chips — flow visibility with no browser timer.
	let prev = $state<{ messages: number; streamMessages: Record<string, number> } | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0;

	const unauthorized = $derived(overview === null && settled && lastStatus === 401);
	const forbidden = $derived(overview === null && settled && lastStatus === 403);
	const unavailable = $derived(overview === null && settled && lastStatus === 501);
	// -1 = the client-side parse boundary rejected the BFF payload (contract drift, not a monitor outage).
	const drifted = $derived(overview === null && settled && lastStatus === -1);
	// 0 stays IN the offline set: after the first settle, a fetch-level failure (network down, BFF timeout)
	// reports status 0 and must render as offline, not hang on the loading message (audit finding).
	const offline = $derived(
		overview === null && settled && ![-1, 200, 401, 403, 501].includes(lastStatus),
	);

	async function load(): Promise<void> {
		const seq = ++inflight;
		const res = await requestJSON<unknown>('/api', 'jetstream');
		if (seq !== inflight) return; // latest-wins
		settled = true;
		if (res.ok) {
			try {
				// Wire boundary: parse (don't cast) the trimmed shape, so a BFF/schema drift surfaces as an
				// honest unreachable state instead of a UI built from values that lie about their type.
				const next = parse(JetStreamOverviewSchema, res.data);
				// Snapshot the OUTGOING poll's counts before replacing it — the delta baseline. A failed
				// interim read keeps the last good baseline (the delta is always against what is on screen).
				if (overview !== null) {
					prev = {
						messages: overview.totals.messages,
						streamMessages: Object.fromEntries(
							overview.streams.map((s) => [s.name, s.state.messages]),
						),
					};
				}
				overview = next;
				lastStatus = 200;
			} catch (err) {
				// -1 = contract drift (the drifted state), distinct from a monitor outage's 502 (audit nit).
				console.error(`jetstream overview parse failure: ${String(err)}`);
				overview = null;
				lastStatus = -1;
			}
		} else {
			// Clear the stale view on failure so the auth/forbidden/offline state reflects reality — else a
			// session that expires mid-view would keep showing the old (privileged) topology.
			overview = null;
			lastStatus = res.status;
		}
	}

	// Live on the JETSTREAM cursor, and this is the surface that proves why neither event feed would do.
	// This panel exists to show a wedged or missing consumer — and a wedged consumer emits no lineage
	// events and no control events, so either feed would leave the panel frozen in precisely the incident
	// it is here to catch. JetStream depth is a gauge with no change-event source, so its live feed is a
	// server-side probe of the NATS monitor's own totals (see feeds.remote.ts), yielded only when they
	// move. Estate-admin gated in the generator, because /jsz is unauthenticated by design.
	//
	// Before this the panel read once on mount and then sat still: a backlog draining and a backlog
	// wedged looked exactly the same until someone clicked Refresh.
	liveRead(jetstreamCursor, () => load());

	// The lineage transactional-outbox DLQ stream (#83) — visually flagged so an operator scanning the
	// fabric spots dead-letter backlog at a glance.
	const isDlq = (name: string) => name === 'DLQ' || name.startsWith('DLQ_');

	// "+N since the last change" deltas — no chip until a baseline exists or when nothing changed. A
	// NEGATIVE delta (messages left the stream — retention, purge, GC or a replay drain) renders
	// neutral, not in the growth green, with a tooltip that says what it actually measures.
	const totalsDelta = $derived(
		prev !== null && overview !== null ? overview.totals.messages - prev.messages : 0,
	);
	function streamDelta(name: string, messages: number): number {
		if (prev === null) return 0;
		const before = prev.streamMessages[name];
		return before === undefined ? 0 : messages - before;
	}
	const fmtDelta = (d: number): string => (d > 0 ? `+${d.toLocaleString()}` : d.toLocaleString());
	const deltaTitle = (d: number): string =>
		d > 0
			? 'messages added since the last change'
			: 'messages removed since the last change (retention, purge, or a drain)';

	function fmtBytes(n: number): string {
		if (n < 1024) return `${n} B`;
		if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
		if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
		return `${(n / 1024 ** 3).toFixed(1)} GiB`;
	}
	/** int64 nanoseconds → compact duration; 0 means unlimited in JetStream configs. */
	function fmtAgeNs(ns: number): string {
		if (ns <= 0) return '∞';
		const s = ns / 1e9;
		if (s < 60) return `${Math.round(s)}s`;
		if (s < 3600) return `${Math.round(s / 60)}m`;
		if (s < 86400) return `${Math.round(s / 3600)}h`;
		return `${Math.round(s / 86400)}d`;
	}
</script>

<div class="page">
	<header>
		<Layers size={16} />
		<h1>Streams</h1>
		<span class="sub mono">JetStream streams · consumer lag · read-only from the NATS monitor</span>
	</header>

	<div class="bar">
		<button class="btn" onclick={load}><RefreshCw size={13} /> Refresh</button>
		{#if overview}
			<span class="totals mono">
				{overview.totals.streams} streams · {overview.totals.consumers} consumers ·
				{overview.totals.messages.toLocaleString()} msgs
				{#if totalsDelta !== 0}
					<span class="delta" class:neg={totalsDelta < 0} title={deltaTitle(totalsDelta)}
						>{fmtDelta(totalsDelta)}</span
					>
				{/if}
				· {fmtBytes(overview.totals.bytes)}
			</span>
		{/if}
	</div>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} /> <a href={loginHref} data-sveltekit-reload>Sign in</a> to view JetStream streams.
		</div>
	{:else if forbidden}
		<div class="empty">
			<ShieldAlert size={15} /> The stream view is estate-admin only — it maps the whole event fabric.
		</div>
	{:else if unavailable}
		<div class="empty">The stream view needs the NATS monitor (NATS_MONITOR_API unset).</div>
	{:else if drifted}
		<div class="empty">
			<ShieldAlert size={15} /> The overview payload drifted from the contract — refusing to render it.
		</div>
	{:else if offline}
		<div class="empty"><RefreshCw size={15} /> NATS monitor unreachable (HTTP {lastStatus}).</div>
	{:else if overview === null}
		<div class="empty">Loading the JetStream overview…</div>
	{:else}
		{#if overview.missing.length > 0}
			<!-- The dead-subscription detector: an EXPECTED consumer group that is absent from the live
			     topology. Raw /jsz cannot show an absence — this diff (chart-rendered expectations vs live
			     consumers) is what catches a Ready pod whose subscription silently died. -->
			<div class="missingbanner" role="alert" aria-label="Missing expected consumers">
				<TriangleAlert size={15} />
				<span>
					<strong>
						{overview.missing.length} expected consumer{overview.missing.length === 1 ? '' : 's'} MISSING</strong
					>
					— a dead subscription: the app may look Ready while nothing reads its stream.
					{#each overview.missing as m (`${m.stream}:${m.service}`)}
						<span
							class="misspair mono"
							title={m.unbound
	? 'a consumer for this group exists (e.g. an orphaned durable) but nothing is attached to it'
	: 'no consumer for this group exists on the stream'}
							>{m.stream}:{m.service}{m.unbound ? ' · present but unbound' : ''}</span
						>
					{/each}
				</span>
			</div>
		{/if}
		{#if overview.streams.length === 0}
			<div class="empty">No JetStream streams exist yet — the event fabric is empty.</div>
		{:else}
			<div class="cards">
				{#each overview.streams as s (s.name)}
					<section class="card" class:dlq={isDlq(s.name)} aria-label="Stream {s.name}">
						<div class="card-head">
							<span class="name mono">{s.name}</span>
							{#if isDlq(s.name)}
								<span
									class="badge dlqbadge"
									title="Lineage dead-letter stream — backlog here is at-risk events"
								>
									DLQ
								</span>
							{/if}
							<span class="badge">{s.retention}</span>
							<span class="badge">{s.storage}</span>
							<span class="spacer"></span>
							<span class="stat mono">
								{s.state.messages.toLocaleString()} msgs
								{#if streamDelta(s.name, s.state.messages) !== 0}
									<span
										class="delta"
										class:neg={streamDelta(s.name, s.state.messages) < 0}
										title={deltaTitle(streamDelta(s.name, s.state.messages))}
										>{fmtDelta(streamDelta(s.name, s.state.messages))}</span
									>
								{/if}
								· {fmtBytes(s.state.bytes)} · seq
								{s.state.first_seq}–{s.state.last_seq} · max_age {fmtAgeNs(s.max_age_ns)} · R{s.num_replicas}
							</span>
						</div>
						<div class="subjects mono">{s.subjects.join(', ') || '—'}</div>
						{#if s.consumers.length === 0}
							<div class="noconsumers">No consumers bound ({s.state.consumer_count} reported).</div>
						{:else}
							<!-- Goal cond 4: the consumer rows on the shared DataTable (sortable; same
							     pressure/wedge/stale semantics, judged against the monitor's clock). -->
							<StreamConsumers consumers={s.consumers} now={overview.now} stream={s.name} />
						{/if}
					</section>
				{/each}
			</div>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 980px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 16px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.bar {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.totals {
		font-size: 12px;
		color: var(--mut);
	}
	.delta {
		color: var(--ok, #3f9e63);
		font-weight: 600;
		margin-left: 2px;
	}
	/* Shrinkage is not growth: a negative delta (retention/purge/drain) renders neutral, not green. */
	.delta.neg {
		color: var(--mut);
	}
	.missingbanner {
		display: flex;
		align-items: baseline;
		gap: 8px;
		border: 1px solid color-mix(in srgb, var(--warn, #d18b28) 60%, var(--line));
		background: color-mix(in srgb, var(--warn, #d18b28) 9%, transparent);
		color: var(--warn, #d18b28);
		border-radius: var(--radius-sm);
		font-size: 13px;
		padding: 10px 12px;
		margin-bottom: 12px;
	}
	.missingbanner strong {
		font-weight: 700;
	}
	.misspair {
		display: inline-block;
		border: 1px solid color-mix(in srgb, var(--warn, #d18b28) 55%, var(--line));
		border-radius: var(--radius-sm);
		font-size: 11px;
		padding: 0 6px;
		margin-left: 6px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 13px;
		padding: 30px 0;
	}
	.cards {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.card {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel-2);
		padding: 12px 14px;
	}
	.card.dlq {
		border-color: color-mix(in srgb, var(--warn, #d18b28) 55%, var(--line));
	}
	.card-head {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.name {
		font-size: 13px;
		font-weight: 600;
		color: var(--ink);
	}
	.badge {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--mut);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 1px 6px;
	}
	.badge.dlqbadge {
		color: var(--warn, #d18b28);
		border-color: color-mix(in srgb, var(--warn, #d18b28) 60%, var(--line));
	}
	.spacer {
		flex: 1;
	}
	.stat {
		font-size: 11px;
		color: var(--faint);
	}
	.subjects {
		font-size: 11px;
		color: var(--mut);
		margin: 6px 0 8px;
	}
	.noconsumers {
		font-size: 12px;
		color: var(--faint);
		padding: 4px 0;
	}
</style>
