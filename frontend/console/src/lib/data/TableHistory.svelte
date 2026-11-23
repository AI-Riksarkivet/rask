<script lang="ts">
	// #113 the commit log — one scannable table answering what changed and when, for every version
	// of a catalog table. It replaces the old three-column "Versions, branches & tags" block, which showed
	// `version | committed | manifest` for the newest ten and hid the rest.
	//
	// Where each column comes from, and why it can only come from there:
	//  · WHAT — Lance's own transaction log (`GET /management/v1/table/{id}/history`). Its `operation` is the FORMAT's
	//    vocabulary (`Overwrite`/`Append`/`Delete`/`Update`/`Merge`/`Rewrite`/`CreateIndex`/`Restore`), and
	//    its detail fields are whatever that operation carries — absent, not null, when it does not. So
	//    "what changed" is rendered from the keys present (see `summarizeChange`), never from a switch on
	//    the operation name.
	//  · WHEN — the manifest's `timestamp_millis` (unambiguous epoch → `…Z`). The history row's own
	//    `timestamp` is an offset-less local-clock string; handing it to `new Date()` would silently render
	//    every commit in the browser's page. It is only ever shown verbatim, as the fallback.
	//
	// The rows themselves come from the FORMAT: the versions the catalog reports are the only versions
	// that exist.
	//
	// This file holds the read, the filters and the selection; the table view is `CommitLogTable`, the
	// selected commit's fields and actions `CommitPanel`, and the branches and tags `TableRefs`.
	import { Button } from '@riksarkivet/ui/button';
	import { Checkbox } from '@riksarkivet/ui/checkbox';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { RefreshCw } from '@lucide/svelte';
	import CommitLogTable from './CommitLogTable.svelte';
	import CommitPanel from './CommitPanel.svelte';
	import { isSchemaChange, summarizeChange, type LogRow } from './history';
	import { fetchTableLog } from './remote/catalog.api';
	import TableRefs from './TableRefs.svelte';

	type Manifest = {
		version?: number;
		timestamp_millis?: number | null;
		manifest_size?: number | null;
		e_tag?: string | null;
	};

	let {
		table,
		manifests,
		tags,
		branches,
		currentVersion,
		onchange,
	}: {
		table: string;
		/** The version list from the page's detail aggregate (ascending) — the format's own answer to
		 *  "which versions exist", and the source of the unambiguous commit timestamps. */
		manifests: Manifest[];
		tags: [string, { version?: number; branch?: string | null }][];
		branches: [string, { createAt?: number; manifestSize?: number | null }][];
		/** The version the page believes is current, for the restore staleness check. */
		currentVersion: number | null;
		/** Reload the page's aggregate after a write (a restore/tag/branch changes it). */
		onchange: () => void;
	} = $props();

	const LIMITS = [
		{ value: '50', label: 'newest 50' },
		{ value: '200', label: 'newest 200' },
		{ value: '1000', label: 'newest 1000' },
	];

	let limit = $state('50');
	// The log, held per (table, limit) through the read layer: a new limit is a new read, and only the
	// read for what is on screen renders. A payload that drifts from the contract rejects; say so —
	// rendering from a half-understood payload is how a log starts lying.
	const log = $derived(fetchTableLog({ table, limit: Number(limit) }));
	const loading = $derived(log.loading);
	const answer = $derived(log.error === undefined ? log.current : undefined);
	const history = $derived(answer?.ok ? answer.data.versions : null);
	const historyStatus = $derived(answer === undefined ? 0 : answer.ok ? 200 : answer.status);
	const historyDetail = $derived(answer !== undefined && !answer.ok ? answer.detail : '');
	const historyDrift = $derived(
		log.error === undefined
			? null
			: `history response drifted from the contract: ${String(log.error)}`,
	);
	/** Read the log again; a failure is held on the read (`log.error`) and rendered from there. */
	function reloadLog(): Promise<void> {
		return log.refresh().catch(() => undefined);
	}

	const txnByVersion = $derived(new Map((history ?? []).map((row) => [row.version, row])));
	const manifestByVersion = $derived(
		new Map(
			manifests
				.filter((m): m is Manifest & { version: number } => m.version != null)
				.map((m) => [m.version, m]),
		),
	);
	const tagsByVersion = $derived(
		tags.reduce<Record<number, string[]>>((acc, [name, tag]) => {
			if (tag.version != null) (acc[tag.version] ??= []).push(name);
			return acc;
		}, {}),
	);

	// The log's rows are the versions the FORMAT reports, newest first: the history endpoint when it answers
	// (it is the paginated source), else the detail aggregate's manifest list.
	const rows = $derived.by((): LogRow[] => {
		const versions =
			history !== null
				? history.map((r) => r.version)
				: [...manifestByVersion.keys()].sort((a, b) => b - a);
		return versions.map((version) => {
			const txn = txnByVersion.get(version) ?? null;
			const manifest = manifestByVersion.get(version);
			return {
				version,
				txn,
				committed: manifest?.timestamp_millis ?? null,
				naive: txn?.timestamp ?? null,
				manifestSize: manifest?.manifest_size ?? null,
				eTag: manifest?.e_tag ?? null,
				refs: tagsByVersion[version] ?? [],
				operation: txn?.operation ?? null,
				change: txn ? summarizeChange(txn) : [],
				schemaChange: txn ? isSchemaChange(txn) : false,
			};
		});
	});

	// ── filters (the payoff of having an operation column at all) ──
	let opFilter = $state('');
	let schemaOnly = $state(false);
	let search = $state('');

	const opCounts = $derived.by(() => {
		const counts = rows.reduce<Record<string, number>>((acc, row) => {
			const key = row.operation ?? 'unreadable';
			acc[key] = (acc[key] ?? 0) + 1;
			return acc;
		}, {});
		return Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
	});

	const haystack = (row: LogRow): string =>
		[
			`v${row.version}`,
			row.operation ?? '',
			row.change.map((b) => b.text).join(' '),
			row.refs.join(' '),
		]
			.join(' ')
			.toLowerCase();

	const filtered = $derived(
		rows.filter((row) => {
			if (opFilter && (row.operation ?? 'unreadable') !== opFilter) return false;
			if (schemaOnly && !row.schemaChange) return false;
			const q = search.trim().toLowerCase();
			return q === '' || haystack(row).includes(q);
		}),
	);

	// ── selection: the row's actions and its raw fields hang off the selected commit (`CommitPanel`), the
	// way the reference console anchors its actions menu — a dense log stays scannable and the detail has
	// room. ──
	let selectedVersion = $state<number | null>(null);
	const selected = $derived(filtered.find((r) => r.version === selectedVersion) ?? null);
	function select(version: number): void {
		selectedVersion = selectedVersion === version ? null : version;
	}
	let restoreNote = $state<string | null>(null);

	async function restored(): Promise<void> {
		selectedVersion = null;
		await reloadLog();
		onchange();
	}

	const summary = $derived(
		`${filtered.length} of ${rows.length} commit${rows.length === 1 ? '' : 's'} shown${
			history !== null && history.length === Number(limit)
				? ` · reading the newest ${limit} — raise the limit to read further back`
				: ''
		}`,
	);

	const versionOptions = $derived(
		rows.map((r) => ({ value: String(r.version), label: `v${r.version}` })),
	);
</script>

<section class="hist">
	<h2>Commit log</h2>
	<p class="mut">
		Every version of this table, newest first — read out of the Lance transaction log.
	</p>
	<p class="summary" data-testid="versions-summary">
		{#if currentVersion !== null}<span>Latest <span class="mono">v{currentVersion}</span></span
			>{/if}
		<span>{tags.length} tag{tags.length === 1 ? '' : 's'}</span>
		<span>{branches.length} branch{branches.length === 1 ? '' : 'es'}</span>
	</p>

	{#if historyDrift}
		<div class="banner fail">{historyDrift}</div>
	{:else if historyStatus === 404}
		<div class="banner warn">
			The catalog does not serve <code>/management/v1/table/{table}/history</code> (404) — this
			deployment predates the endpoint. Showing the manifest list only: <em>operation</em>,
			<em>what changed</em> and the raw transaction fields need it.
		</div>
	{:else if historyStatus === 403}
		<div class="banner warn">
			Denied: reading the commit log needs metadata access (<code>can_get_metadata</code>). Showing
			the manifest list only.
		</div>
	{:else if historyStatus === 401}
		<div class="banner warn">Sign in to read the commit log — showing the manifest list only.</div>
	{:else if historyStatus === 0 && !loading}
		<div class="banner warn">
			The catalog did not answer the commit-log request — showing the manifest list only.
		</div>
	{:else if historyStatus !== 200 && historyStatus !== 0}
		<div class="banner warn">
			Commit log unavailable (HTTP {historyStatus}{historyDetail ? ` — ${historyDetail}` : ''}) —
			showing the manifest list only.
		</div>
	{/if}

	<div class="toolbar">
		<!-- Without the commit log there are no operations to filter BY: every row is a manifest with a null
		     operation, so the chip row would offer one bucket that filters nothing and name it after a
		     failure ("unreadable") that did not happen. Same for the schema toggle. -->
		{#if history !== null}
			<div class="chips" role="group" aria-label="Filter by operation">
				<Button
					variant="ghost"
					size="xs"
					aria-pressed={opFilter === ''}
					onclick={() => (opFilter = '')}>all {rows.length}</Button
				>
				{#each opCounts as [op, n] (op)}
					<Button
						variant="ghost"
						size="xs"
						aria-pressed={opFilter === op}
						onclick={() => (opFilter = opFilter === op ? '' : op)}>{op} {n}</Button
					>
				{/each}
			</div>
			<label class="toggle">
				<Checkbox bind:checked={schemaOnly} />
				schema changes only
			</label>
		{/if}
		<Input
			class="h-7 min-w-30 flex-[1_1_160px] font-mono text-xs"
			bind:value={search}
			placeholder="search predicate, tag…"
			aria-label="Search the log"
		/>
		<Select bind:value={limit} ariaLabel="How many commits to read" options={LIMITS} />
		<Button variant="ghost" size="xs" onclick={reloadLog} disabled={loading}>
			<RefreshCw size={11} />
			{loading ? '…' : 'reload'}
		</Button>
	</div>

	<CommitLogTable
		rows={filtered}
		empty={rows.length === 0}
		loading={loading && history === null && manifests.length === 0}
		historyRead={history !== null}
		{currentVersion}
		{selectedVersion}
		onselect={select}
		{summary}
	/>
	{#if restoreNote}
		<!-- The advisory outlives the panel: a successful restore closes the selection, and "this write was
		     NOT checked against the current tip" is exactly the sentence that must not vanish with it. -->
		<p class="mut">{restoreNote}</p>
	{/if}

	{#if selected}
		{#key selected.version}
			<CommitPanel
				{table}
				row={selected}
				{currentVersion}
				onclose={() => (selectedVersion = null)}
				onrestored={restored}
				onnote={(note) => (restoreNote = note)}
				{onchange}
			/>
		{/key}
	{/if}

	<TableRefs {table} {tags} {branches} {versionOptions} {onchange} />
</section>

<style>
	.hist {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
		margin: 0 0 8px;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.banner {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 12px;
		padding: 6px 10px;
		margin-bottom: 10px;
	}
	.banner.warn {
		border-color: color-mix(in oklab, var(--warning) 45%, transparent);
		background: color-mix(in oklab, var(--warning) 12%, transparent);
	}
	.banner.fail {
		border-color: color-mix(in oklab, var(--destructive) 45%, transparent);
		background: color-mix(in oklab, var(--destructive) 12%, transparent);
	}
	.toolbar {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
		margin-bottom: 10px;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.toggle {
		align-items: center;
		color: var(--muted-foreground);
		display: flex;
		font-size: 12px;
		gap: 4px;
	}
	.summary {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 16px;
		margin: 0 0 12px;
		font-size: 13px;
	}
	.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
	code {
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 11px;
	}
</style>
