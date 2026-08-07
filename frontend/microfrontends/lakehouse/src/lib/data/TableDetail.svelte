<script lang="ts">
	// `/tables/<id>` — the catalog table-detail view (#52): schema, stats, version + tag history, the
	// #50 maintenance policy (owner-gated set/delete), and the #51 access review. Data comes in one
	// round-trip through the `fetchTableDetail` remote query (the six-read fan-out still happens on the
	// zone server); every write is a remote command carrying the signed-in user's bearer. A dataset the
	// catalog does not register (e.g. a storage-managed medallion zone) renders the honest
	// not-in-catalog state instead of a broken page.
	//
	// #98 SPLIT: each admin workflow lives in `./table-detail/` as its own component owning its own
	// state (SchemaSection, InsertRowsSection, RowOpsSection, BlobPreviewSection, IndexesSection,
	// MaintenanceSection, DangerZone, RecoverCard). This file owns the ONE read (`load`), the derived
	// part-splits, the tab bar and the page states. The workflow sections mount under `{#key table}`
	// (the TableHistory precedent), so a navigation REMOUNTS them: every editor, draft and armed
	// confirm belongs to one table and resets by construction — which retired the 30-line hand reset
	// this file used to carry.
	import { GrantsPanel, type GrantsClient } from '@rask/ui/grants-panel';
	import { Database, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import AccessGraph from './AccessGraph.svelte';
	import { fetchProducers } from '$lib/api';
	import type { ProducerInfo } from '@rask/api/lineage';
	import {
		checkAccess,
		fetchAccess,
		grantAccess,
		revokeAccess,
	} from './remote/access-objects.remote';
	import { deriveQuality, type QualityBadge } from '$lib/quality';
	import ReadersPanel from '$lib/ReadersPanel.svelte';
	import { partErrored, type Policy, type TableStats, type TableDetail } from './catalog';
	import { fetchTableDetail } from './remote/catalog.remote';
	import DetailTabs from './DetailTabs.svelte';
	import { fmtBytes } from './history';
	import TableHistory from './TableHistory.svelte';
	import StageBadge from './StageBadge.svelte';
	import { stageOfTable } from './stage';
	import TablePreview from './TablePreview.svelte';
	import TableProperties from './TableProperties.svelte';
	import BlobPreviewSection from './table-detail/BlobPreviewSection.svelte';
	import DangerZone from './table-detail/DangerZone.svelte';
	import IndexesSection from './table-detail/IndexesSection.svelte';
	import InsertRowsSection from './table-detail/InsertRowsSection.svelte';
	import MaintenanceSection from './table-detail/MaintenanceSection.svelte';
	import RecoverCard from './table-detail/RecoverCard.svelte';
	import RowOpsSection from './table-detail/RowOpsSection.svelte';
	import SchemaSection from './table-detail/SchemaSection.svelte';
	import type { SchemaField } from './table-detail/type-name';

	let { table }: { table: string } = $props();

	// Goal cond 3: the FGA view lives on an Access TAB (overview stays the default); the medallion
	// stage badge is derived from the table's namespace segment. Goal cond 5 adds the Preview tab.
	// #113 adds History — the commit log, which took the version/branch/tag surface off overview.
	// `?tab=` deep-links a tab (the registry drawer's "access" jump uses it) and the tab bar now WRITES it,
	// so a tab is linkable, reload-stable and back-button-able.
	const TABS = ['overview', 'preview', 'history', 'access'];
	// DERIVED from the URL, not mirrored into it: the tab is a linkable, reload-stable, back-button-able
	// piece of the address, and deriving it is the only way that cannot race. (Mirroring `tab` into the URL
	// from an effect left the deep link at the mercy of effect order — on reload the mirror stripped
	// `?tab=history` before the reader saw it, and the page opened on overview.)
	const tab = $derived.by(() => {
		const wanted = page.url.searchParams.get('tab') ?? 'overview';
		return TABS.includes(wanted) ? wanted : 'overview';
	});
	function selectTab(next: string): void {
		const url = new URL(page.url);
		if (next === 'overview') url.searchParams.delete('tab');
		else url.searchParams.set('tab', next);
		goto(url, { replaceState: true, keepFocus: true, noScroll: true });
	}
	const stageInfo = $derived(stageOfTable(table));

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	// The zone-owned catalog seam the shared @rask/ui GrantsPanel calls (the lib never owns an API
	// client). The panel's positional signature is bound to the remote functions' single argument here.
	const grantsClient: GrantsClient = {
		fetchAccess: (kind, id) => fetchAccess({ kind, id }),
		checkAccess: (kind, id, user, relation) => checkAccess({ kind, id, user, relation }),
		grantAccess: (kind, id, user, relation) => grantAccess({ kind, id, user, relation }),
		revokeAccess: (kind, id, user, relation) => revokeAccess({ kind, id, user, relation }),
	};

	let detail = $state<TableDetail | null>(null);
	let lastStatus = $state(0);

	// #81 the SvelteFlow authorization graph is lazy-mounted (heavy) behind this toggle.
	let showGraph = $state(false);

	// scope #6 quality gate — the validator's latest dataQualityAssertions verdict for this dataset, from
	// the lineage service's producing runs (medallion stages record it; a plain catalog table has none).
	let quality = $state<QualityBadge>(null);
	// The same runs, kept rather than thrown away: #113's "by whom" column joins them to versions, so the
	// commit log costs no extra request. `null` = the lineage store did not answer at all.
	let producerRuns = $state<ProducerInfo[] | null>(null);

	async function loadQuality(): Promise<void> {
		const current = table;
		const res = await fetchProducers(current);
		if (table !== current) return; // latest-wins
		quality = deriveQuality(res);
		producerRuns = res?.producers ?? null;
	}

	const unauthorized = $derived(detail === null && lastStatus === 401);
	const notInCatalog = $derived(detail === null && lastStatus === 404);
	const denied = $derived(detail === null && lastStatus === 403);
	const offline = $derived(detail === null && ![0, 200, 401, 403, 404].includes(lastStatus));

	async function load(): Promise<void> {
		// Latest-wins: the user may navigate table A→B while A's request is in flight (the route reuses
		// this instance), so drop a response for a table we have already navigated away from.
		const requested = table;
		const res = await fetchTableDetail({ table: requested });
		if (table !== requested) return;
		if (res.ok) {
			detail = res.data;
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	$effect(() => {
		// Reset THIS component's state on a table change. The workflow sections need no entry here:
		// they mount under `{#key table}` below, so a navigation destroys and recreates them — an
		// editor opened on A cannot survive into B by construction.
		void table;
		detail = null;
		lastStatus = 0;
		showGraph = false;
		quality = null;
		producerRuns = null;
		load();
		loadQuality();
	});

	// Split each part into "resolved value" vs "upstream failed" so the markup can render an honest
	// "unavailable" instead of an affirmative empty state (which for policy would invite an overwrite).
	const stats = $derived(
		partErrored(detail?.stats) ? null : ((detail?.stats ?? null) as TableStats | null),
	);
	const policy = $derived(
		partErrored(detail?.policy) ? null : ((detail?.policy ?? null) as Policy | null),
	);
	const policyUnavailable = $derived(partErrored(detail?.policy));
	const schemaFields = $derived((detail?.describe.schema?.fields ?? []) as SchemaField[]);
	// #74 tail — the table's schema-level metadata map (what schema_metadata/update sets), for the properties
	// editor. `describe.metadata` is the current map; absent → an empty, still-editable map.
	const tableMeta = $derived((detail?.describe.metadata ?? {}) as Record<string, string>);
	// #78 — `description` is the reserved property: the table's own prose. Rendered here READ-ONLY, under
	// the name; editing it stays in the Properties section, so there is one editor and one door.
	const tableDescription = $derived((tableMeta.description ?? '').trim());
	const versions = $derived(
		partErrored(detail?.versions)
			? []
			: ((detail?.versions?.versions ?? []) as {
					version?: number;
					timestamp_millis?: number | null;
					manifest_size?: number | null;
					e_tag?: string | null;
				}[]),
	);
	const tags = $derived(
		partErrored(detail?.tags)
			? []
			: Object.entries((detail?.tags?.tags ?? {}) as Record<string, { version?: number }>),
	);
	// Lance branches: a name → BranchContents map (createAt in seconds, manifestSize in bytes).
	const branches = $derived(
		partErrored(detail?.branches)
			? []
			: Object.entries(
					(detail?.branches?.branches ?? {}) as Record<
						string,
						{ createAt?: number; manifestSize?: number | null }
					>,
				),
	);
	// Indexes on the table (#64) — scalar/vector, each over one or more columns.
	const indexes = $derived(
		partErrored(detail?.indexes)
			? []
			: ((detail?.indexes?.indexes ?? []) as {
					index_name?: string;
					columns?: string[];
					index_type?: string | null;
				}[]),
	);
</script>

<div class="page">
	<header>
		<Database size={16} />
		<h1 class="mono">{table}</h1>
		{#if stageInfo}<StageBadge info={stageInfo} />{/if}
		{#if detail?.describe.version != null}
			<span class="sub mono">v{detail.describe.version}</span>
		{/if}
	</header>
	{#if tableDescription}
		<p class="tdesc">{tableDescription}</p>
	{/if}

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view table details.
			</p>
		</div>
	{:else if notInCatalog}
		<!-- #75: a 404 here is exactly where someone lands right after dropping a table, so this is
		     where recovery belongs. RecoverCard asks the catalog's trash for a record and renders the
		     recover offer or the honest not-registered copy itself; keyed so a navigation between two
		     404s cannot carry one table's record (or a failed undrop's error) onto the next. -->
		{#key table}
			<RecoverCard {table} onrecovered={load} />
		{/key}
	{:else if denied}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				You don't have read access to this table's catalog metadata — its lineage is on the <a
					href="/lakehouse/lineage"
					data-sveltekit-reload>explorer</a
				>.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if detail === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<!-- Goal cond 3: overview (default) | preview (goal cond 5) | history (#113) | access. -->
		<DetailTabs tabs={TABS} active={tab} onselect={selectTab} />
		{#if tab === 'preview'}
			<TablePreview {table} />
		{:else if tab === 'history'}
			<!-- #113 the commit log. `producers` is the SAME fetch the quality badge already made, so the
			     author join costs no extra request. Keyed by table so a navigation REMOUNTS it: every
			     selection, armed confirm and filter belongs to one table and resets by construction. -->
			{#key table}
				<TableHistory
					{table}
					manifests={versions}
					{tags}
					{branches}
					producers={producerRuns}
					currentVersion={detail.describe.version ?? null}
					onchange={load}
				/>
			{/key}
		{:else if tab === 'access'}
			<section>
				<h2>Access</h2>
				<GrantsPanel dataset={table} client={grantsClient} />
				<ReadersPanel dataset={table} />
				<!-- #81 the relationship graph is heavy (SvelteFlow) — lazy-mount behind a toggle. -->
				<button class="btn ghost graphtoggle" onclick={() => (showGraph = !showGraph)}>
					{showGraph ? 'Hide' : 'Show'} authorization graph
				</button>
				{#if showGraph}<AccessGraph dataset={table} />{/if}
			</section>
		{/if}
		<!-- The overview is HIDDEN on the other tabs, not unmounted — the original kept every section's
		     state in the always-mounted parent, so a typed draft, an armed confirm or an unread result
		     message survived an overview → history → overview round trip. The split moved that state
		     into the children, so an {#if} here would destroy it on every tab flip (caught by the #98
		     adversarial review); `hidden` keeps the instances (and their state) alive while `{#key
		     table}` below still resets them on a table NAVIGATION, which is the one reset that is
		     wanted. -->
		<div hidden={tab !== 'overview'}>
			<section>
				<h2>Stats</h2>
				{#if partErrored(detail.stats)}
					<p class="mut">Stats unavailable right now.</p>
				{:else}
					<div class="stats mono">
						<span>{stats?.num_rows ?? '—'} rows</span>
						<span>{fmtBytes(stats?.total_bytes)}</span>
						<span>{stats?.num_indices ?? 0} indices</span>
						<!-- #78 the catalog's fixed file format (Lance columnar, storage 2.2) — never a silent guess. -->
						{#if detail.format}
							<span
								class="fmt"
								title="This catalog stores Lance only; format-selecting properties are rejected."
								>{detail.format.name} · storage v{detail.format.storage_version}</span
							>
						{/if}
						<!-- scope #6 quality gate — the validator's dataQualityAssertions verdict on the latest
					     producing run (from lineage). A plain catalog table has none, stated honestly. -->
						{#if quality}
							<span
								class="qual {quality.passed ? 'ok' : 'bad'}"
								title="Validator dataQualityAssertions on the latest producing run (lineage)."
								>quality {quality.passed ? 'passed' : 'blocked'}{quality.assertions
									? ` · ${quality.assertions} check${quality.assertions === 1 ? '' : 's'}`
									: ''}</span
							>
						{:else}
							<span class="qual none" title="No producing run has recorded dataQualityAssertions."
								>no quality gate</span
							>
						{/if}
						{#if detail.describe.location}<span class="loc">{detail.describe.location}</span>{/if}
					</div>
				{/if}
			</section>

			<!-- The workflow sections own their state; `{#key table}` is what resets it on navigation
			     (see the header comment). Keep any new section INSIDE this block for the same reason. -->
			{#key table}
				<SchemaSection {table} fields={schemaFields} onchanged={load} />

				<!-- #74 tail — table + per-column property editor (writer-gated; session-only /capi BFF). -->
				<TableProperties {table} fields={schemaFields} {tableMeta} onchange={load} />

				<InsertRowsSection {table} onchanged={load} />
				<RowOpsSection {table} onchanged={load} />
				<BlobPreviewSection {table} fields={schemaFields} />
				<IndexesSection {table} {indexes} onchanged={load} />
				<MaintenanceSection {table} {policy} {policyUnavailable} onchanged={load} />
				<DangerZone {table} />
			{/key}
		</div>
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 18px;
	}
	/* The description belongs TO the name, so it closes the gap under it rather than opening a second one. */
	header:has(+ .tdesc) {
		margin-bottom: 6px;
	}
	.tdesc {
		margin: 0 0 18px;
		max-width: 78ch;
		color: var(--mut);
		font-size: 13px;
		line-height: 1.5;
		white-space: pre-wrap;
	}
	h1 {
		font-size: 18px;
		margin: 0;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	section {
		margin-bottom: 22px;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.stats {
		display: flex;
		flex-wrap: wrap;
		gap: 14px;
		font-size: 12px;
		color: var(--mut);
	}
	.loc {
		color: var(--faint);
	}
	.fmt {
		border: 1px solid color-mix(in srgb, var(--accent, #ffc14d) 45%, var(--line));
		border-radius: var(--radius-sm);
		padding: 0 6px;
		color: var(--mut);
	}
	.qual {
		border-radius: var(--radius-sm);
		padding: 0 6px;
		border: 1px solid var(--line);
	}
	.qual.ok {
		color: var(--ok);
		border-color: color-mix(in srgb, var(--ok) 45%, var(--line));
	}
	.qual.bad {
		color: var(--fail);
		border-color: color-mix(in srgb, var(--fail) 45%, var(--line));
	}
	.qual.none {
		color: var(--faint);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.graphtoggle {
		margin: 10px 0 8px;
	}
</style>
