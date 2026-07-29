<script lang="ts">
	// #113 the commit log — one scannable table answering what changed, by whom, and when, for every version
	// of a catalog table. It replaces the old three-column "Versions, branches & tags" block, which showed
	// `version | committed | manifest` for the newest ten and hid the rest.
	//
	// Where each column comes from, and why it can only come from there:
	//  · WHAT — Lance's own transaction log (`GET /v1/table/{id}/history`). Its `operation` is the FORMAT's
	//    vocabulary (`Overwrite`/`Append`/`Delete`/`Update`/`Merge`/`Rewrite`/`CreateIndex`/`Restore`), and
	//    its detail fields are whatever that operation carries — absent, not null, when it does not. So
	//    "what changed" is rendered from the keys present (see `summarizeChange`), never from a switch on
	//    the operation name.
	//  · WHEN — the manifest's `timestamp_millis` (unambiguous epoch → `…Z`). The history row's own
	//    `timestamp` is an offset-less local-clock string; handing it to `new Date()` would silently render
	//    every commit in the browser's zone. It is only ever shown verbatim, as the fallback.
	//  · WHO — the lineage store's producing run, joined on the version number. Lance has no notion of a
	//    user and must not grow one; identity is our concern. An unjoined version renders `—`, never a
	//    guess: maintenance (GC/compact) writes versions with no governed run, the compaction service
	//    records an EMPTY author, and a client-direct write has no author to record.
	//  · ROWS/BYTES — the same run's measured output statistics. `—` when it measured nothing, never `0`.
	//
	// The rows themselves come from the FORMAT, not from lineage: a producing run can name a version the GC
	// has since reclaimed, and a log that listed those would be listing commits that no longer exist.
	import { Badge } from '@rask/ui/badge';
	import {
		createSvelteTable,
		DataTable,
		DataTableHeaderButton,
		getCoreRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderComponent,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
		type SortingState,
	} from '@rask/ui/data-table';
	import { Select } from '@rask/ui/select';
	import { ChevronDown, ChevronRight, RefreshCw } from '@lucide/svelte';
	import type { ProducerInfo } from '@rask/api/lineage';
	import type { Me } from '@rask/api';
	import { fetchMeViaBff } from '$lib/http';
	import {
		createTableBranch,
		createTableTag,
		deleteTableBranch,
		deleteTableTag,
		fetchTableHistory,
		moveTableTag,
		restoreTableVersion,
		type CatalogResult,
		type HistoryRow,
	} from './catalog';
	import {
		authorFor,
		displayAuthor,
		failedAttempts,
		fmtBytes,
		fmtEpoch,
		fmtInstant,
		isSchemaChange,
		opTone,
		rawFields,
		summarizeChange,
		type Authorship,
		type ChangeBit,
	} from './history';
	import VersionCompare from './VersionCompare.svelte';

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
		producers,
		currentVersion,
		onchange,
	}: {
		table: string;
		/** The version list from the page's detail aggregate (ascending) — the format's own answer to
		 *  "which versions exist", and the source of the unambiguous commit timestamps. */
		manifests: Manifest[];
		tags: [string, { version?: number }][];
		branches: [string, { createAt?: number; manifestSize?: number | null }][];
		/** The producing runs for this dataset, already fetched by the page for its quality badge (so the
		 *  author join costs no extra request). `null` means the lineage store did not answer at all — a
		 *  different fact from "no run recorded this version", and shown as one. */
		producers: ProducerInfo[] | null;
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
	let history = $state<HistoryRow[] | null>(null);
	let historyStatus = $state(0);
	let historyDetail = $state('');
	let historyDrift = $state<string | null>(null);
	let loading = $state(false);
	// A monotonic request token, checked in BOTH the success and the failure path: the log is keyed by
	// (table, limit) and either can change while a request is in flight. The stale state is cleared first so
	// nothing downstream renders the previous table's commits under this table's name.
	let historyToken = 0;

	async function loadHistory(): Promise<void> {
		const forTable = table;
		const forLimit = Number(limit);
		const mine = ++historyToken;
		if (!meAsked) {
			meAsked = true;
			void fetchMeViaBff().then((res) => (me = res));
		}
		loading = true;
		history = null;
		historyStatus = 0;
		historyDetail = '';
		historyDrift = null;
		try {
			const res = await fetchTableHistory(forTable, forLimit);
			if (mine !== historyToken) return;
			if (res.ok) {
				history = res.data.versions;
				historyStatus = 200;
			} else {
				historyStatus = res.status;
				historyDetail = res.detail;
			}
		} catch (err) {
			// The parse boundary throws when the wire drifts from the contract. Say so — rendering from a
			// half-understood payload is how a log starts lying.
			if (mine !== historyToken) return;
			historyDrift = `history response drifted from the contract: ${String(err)}`;
		} finally {
			if (mine === historyToken) loading = false;
		}
	}

	$effect(() => {
		void table;
		void limit;
		void loadHistory();
	});

	// Our own identity, for resolving OUR OWN sub in the author column. Fetched once per mount alongside the
	// first log read — `meAsked` is a plain flag, not state, so reading it here cannot re-trigger the effect.
	let me = $state<Me | null>(null);
	let meAsked = false;

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

	type LogRow = {
		version: number;
		txn: HistoryRow | null;
		committed: number | null;
		naive: string | null;
		manifestSize: number | null;
		eTag: string | null;
		refs: string[];
		operation: string | null;
		change: ChangeBit[];
		schemaChange: boolean;
		who: Authorship | null;
	};

	// The log's rows are the versions the FORMAT reports, newest first: the history endpoint when it answers
	// (it is the paginated source), else the detail aggregate's manifest list. Lineage only ever enriches.
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
				who: authorFor(version, producers),
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
			row.who?.author ?? '',
			row.who?.operation ?? '',
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

	// ── selection: the row's actions and its raw fields hang off the selected commit, the way the reference
	// console anchors its actions menu — a dense log stays scannable and the detail has room. ──
	let selectedVersion = $state<number | null>(null);
	const selected = $derived(filtered.find((r) => r.version === selectedVersion) ?? null);
	function select(version: number): void {
		selectedVersion = selectedVersion === version ? null : version;
		restoreArmed = null;
		restoreTyped = '';
		restoreError = null;
		tagError = null;
		tagName = '';
	}

	// ── restore: our restore MINTS A FRESH VERSION pointing at the old data, so nothing is orphaned and no
	// branch becomes unreachable (which is why this needs none of the reference console's dropped-snapshot
	// warnings). It does mutate the table's tip, so it asks for the version to be typed, and it refuses when
	// the tip moved under the page. ──
	let restoreArmed = $state<number | null>(null);
	let restoreTyped = $state('');
	let restoreBusy = $state(false);
	let restoreError = $state<string | null>(null);
	let restoreNote = $state<string | null>(null);

	const restoreTypedOk = $derived(
		restoreArmed !== null &&
			[String(restoreArmed), `v${restoreArmed}`].includes(restoreTyped.trim().toLowerCase()),
	);

	async function runRestore(version: number): Promise<void> {
		if (restoreBusy) return;
		restoreBusy = true;
		restoreError = null;
		restoreNote = null;
		try {
			// Expected-version precondition, client-side: `RestoreTableRequest` carries only the target, so a
			// restore from a stale page would silently win over a concurrent commit. Re-read the tip first and
			// refuse on a mismatch. Not atomic — the catalog needs an `expected_version` to close the race —
			// and when the endpoint is unavailable the check cannot run at all, which the UI states.
			const tip = await fetchTableHistory(table, 1);
			if (tip.ok) {
				const newest = tip.data.versions[0]?.version ?? null;
				if (newest !== null && currentVersion !== null && newest !== currentVersion) {
					restoreError = `The table moved to v${newest} while this page was open (it showed v${currentVersion}). Reload before restoring — otherwise this would land on top of someone else's commit.`;
					return;
				}
			} else {
				restoreNote =
					'The concurrent-write check could not run (the catalog did not serve the commit log), so this restore was not verified against the current tip.';
			}
			const res = await restoreTableVersion(table, version);
			if (res.ok) {
				restoreArmed = null;
				restoreTyped = '';
				selectedVersion = null;
				await loadHistory();
				onchange();
			} else if (res.status === 401) {
				restoreError = 'Sign in to restore a version.';
			} else if (res.status === 403) {
				restoreError = 'Denied: restoring a version needs the owner tier (can_restore).';
			} else {
				restoreError = res.detail;
			}
		} finally {
			restoreBusy = false;
		}
	}

	// ── tag THIS version (the pin-this affordance, instead of picking a version out of a dropdown) ──
	let tagName = $state('');
	let tagBusy = $state(false);
	let tagError = $state<string | null>(null);

	async function runTag(version: number): Promise<void> {
		const name = tagName.trim();
		if (tagBusy || !name) return;
		tagBusy = true;
		tagError = null;
		try {
			const res = await createTableTag(table, name, version);
			if (res.ok) {
				tagName = '';
				onchange();
			} else if (res.status === 401) {
				tagError = 'Sign in to tag a version.';
			} else if (res.status === 403) {
				tagError = 'Denied: tagging a version needs writer access (can_create_tag).';
			} else {
				tagError = res.detail;
			}
		} finally {
			tagBusy = false;
		}
	}

	// ── refs: tags decorate the versions they pin, branches are their own row. Both deletes are destructive
	// and now ask first — the bare × that deleted a ref on one click was the least guarded control here. ──
	let refBusy = $state(false);
	let refError = $state<string | null>(null);
	let refConfirm = $state<{ kind: 'tag' | 'branch'; name: string } | null>(null);
	let movingTag = $state<string | null>(null);
	let moveTo = $state('');
	let newBranch = $state('');
	let newBranchFrom = $state('');

	async function refDo(fn: () => Promise<CatalogResult<unknown>>): Promise<void> {
		if (refBusy) return;
		refBusy = true;
		refError = null;
		try {
			const res = await fn();
			if (res.ok) onchange();
			else if (res.status === 401) refError = 'Sign in to manage tags & branches.';
			else if (res.status === 403) refError = 'Denied: managing refs needs writer/owner access.';
			else refError = res.detail;
		} finally {
			refBusy = false;
			refConfirm = null;
		}
	}

	async function runMoveTag(): Promise<void> {
		if (!movingTag || !moveTo) return;
		const name = movingTag;
		await refDo(() => moveTableTag(table, name, Number(moveTo)));
		movingTag = null;
		moveTo = '';
	}

	async function runCreateBranch(): Promise<void> {
		const name = newBranch.trim();
		if (!name) return;
		await refDo(() => createTableBranch(table, name, newBranchFrom ? Number(newBranchFrom) : null));
		newBranch = '';
		newBranchFrom = '';
	}

	const versionOptions = $derived(
		rows.map((r) => ({ value: String(r.version), label: `v${r.version}` })),
	);
	const attempted = $derived(failedAttempts(producers));
	// The rows that could not be attributed — the legend is only worth showing when something needs it.
	const unattributed = $derived(rows.filter((r) => r.who?.author == null).length);
	let compareBase = $state('');

	// ── the DataTable (the shared @rask/ui shell over tanstack: sorting + a real page size) ──
	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 25 });

	const columns: ColumnDef<LogRow>[] = [
		{
			id: 'version',
			accessorKey: 'version',
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'version',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(versionCell, row.original),
			meta: { headerClass: 'w-20' },
		},
		{
			id: 'operation',
			accessorFn: (r) => r.operation ?? '',
			header: 'operation',
			cell: ({ row }) => renderSnippet(operationCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'change',
			header: 'what changed',
			cell: ({ row }) => renderSnippet(changeCell, row.original),
		},
		{
			id: 'who',
			header: 'by whom',
			cell: ({ row }) => renderSnippet(whoCell, row.original),
			meta: { headerClass: 'w-36' },
		},
		{
			id: 'when',
			accessorFn: (r) => r.committed ?? 0,
			header: ({ column }) =>
				renderComponent(DataTableHeaderButton, {
					label: 'when',
					sorted: column.getIsSorted(),
					onclick: column.getToggleSortingHandler(),
				}),
			cell: ({ row }) => renderSnippet(whenCell, row.original),
			meta: { headerClass: 'w-36' },
		},
		{
			id: 'size',
			header: 'rows / bytes',
			cell: ({ row }) => renderSnippet(sizeCell, row.original),
			meta: { headerClass: 'w-28' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-16' },
		},
	];

	const logTable = createSvelteTable({
		get data() {
			return filtered;
		},
		columns,
		state: {
			get sorting() {
				return sorting;
			},
			get pagination() {
				return pagination;
			},
		},
		onSortingChange: (u) => (sorting = typeof u === 'function' ? u(sorting) : u),
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});

	const TONE_CLASS: Record<string, string> = {
		append: 'border-transparent bg-success text-success-foreground',
		delete: 'border-transparent bg-destructive text-white',
		rewrite: 'border-transparent bg-warning text-warning-foreground',
		schema: 'border-primary/40 bg-primary/10 text-primary',
		index: 'border-teal-500/40 bg-teal-500/15 text-teal-800 dark:text-teal-200',
		restore: 'border-violet-500/40 bg-violet-500/15 text-violet-800 dark:text-violet-200',
		unknown: 'border-border bg-muted text-muted-foreground',
	};
</script>

{#snippet versionCell(row: LogRow)}
	<span class="vcell">
		<span class="mono v">v{row.version}</span>
		{#if row.version === currentVersion}<span class="tip" title="the table's current version"
		  >tip</span
		>{/if}
		{#each row.refs as name (name)}
			<span class="tagchip mono" title="tag {name} pins v{row.version}">{name}</span>
		{/each}
		{#if row.schemaChange}
			<span class="schemadot" title="this commit changed the schema" aria-label="schema change">◆</span
			>
		{/if}
	</span>
{/snippet}

{#snippet operationCell(row: LogRow)}
	{#if row.operation}
		<Badge variant="outline" class="px-1.5 text-[10px] {TONE_CLASS[opTone(row.operation)]}"
			>{row.operation}</Badge
		>
	{:else}
		<span
			class="mut"
			title={history === null
	? 'the commit log is unavailable — this row is the manifest only'
	: 'the transaction file for this version could not be read'}>—</span
		>
	{/if}
{/snippet}

{#snippet changeCell(row: LogRow)}
	{#if row.change.length > 0}
		<span class="bits" title={row.change.map((b) => b.text).join(' · ')}>
			{#each row.change as bit, i (i)}
				{#if i > 0}<span class="sep">·</span>{/if}
				<span class:mono={bit.mono} title={bit.mono ? bit.text : undefined}>{bit.text}</span>
			{/each}
		</span>
	{:else if row.operation === 'Restore'}
		<span class="mut" title="the catalog does not report the restored version yet"
			>target version not recorded</span
		>
	{:else}
		<span class="mut">—</span>
	{/if}
{/snippet}

{#snippet whoCell(row: LogRow)}
	{#if row.who?.author}
		{@const shown = displayAuthor(row.who.author, me)}
		<span class="who">
			<span class:mono={!shown.isMe} title={shown.title}>{shown.text}</span>
			{#if row.who.operation}<span class="via">via {row.who.operation}</span>{/if}
			{#if row.who.qualityPassed !== null}
				<span class="qual" class:bad={!row.who.qualityPassed}>
					{row.who.qualityPassed ? 'quality passed' : 'quality blocked'}
				</span>
			{/if}
		</span>
	{:else}
		<span class="mut" title="no governed run recorded this version">—</span>
	{/if}
{/snippet}

{#snippet whenCell(row: LogRow)}
	{#if row.committed != null}
		<span class="mono when">{fmtEpoch(row.committed, 'ms')}</span>
	{:else if row.naive}
		<!-- The wire's own string, verbatim: it carries no offset, so this view will not pretend to know
		     which zone it is in by reformatting it. -->
		<span
			class="mono naive when"
			title="the catalog reports this commit time without a timezone offset">{row.naive}</span
		>
	{:else}
		<span class="mut">—</span>
	{/if}
{/snippet}

{#snippet sizeCell(row: LogRow)}
	{#if row.who?.rowCount != null || row.who?.sizeBytes != null}
		<span class="mono size">
			{row.who.rowCount != null ? `${row.who.rowCount} rows` : '— rows'}
			<span class="sep">·</span>
			{fmtBytes(row.who.sizeBytes)}
		</span>
	{:else}
		<span class="mut" title="the run that wrote this version measured no output statistics">—</span>
	{/if}
{/snippet}

{#snippet actionsCell(row: LogRow)}
	<!-- A chevron, not the word "details": seven columns of real content already fill the card, and the
	     word cost more width than the whole actions column is worth. The accessible name still says what it
	     opens, and the row itself is clickable. -->
	<button
		class="btn tiny ghost chev"
		aria-label="details for v{row.version}"
		aria-expanded={selectedVersion === row.version}
		onclick={(e) => {
	e.stopPropagation();
	select(row.version);
}}
	>
		{#if selectedVersion === row.version}<ChevronDown size={13} />{:else}<ChevronRight size={13} />{/if}
	</button>
{/snippet}

<section class="hist">
	<h2>Commit log</h2>
	<p class="mut">
		Every version of this table, newest first — read out of the Lance transaction log, with the
		governed run that wrote each version joined on the version number.
	</p>

	{#if historyDrift}
		<div class="banner fail">{historyDrift}</div>
	{:else if historyStatus === 404}
		<div class="banner warn">
			The catalog does not serve <code>/v1/table/{table}/history</code> (404) — this deployment
			predates the endpoint. Showing the manifest list only: <em>operation</em>, <em>what changed</em> and
			the raw transaction fields need it.
		</div>
	{:else if historyStatus === 403}
		<div class="banner warn">
			Denied: reading the commit log needs metadata access (<code>can_get_metadata</code>). Showing the
			manifest list only.
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
				<button
					class="fchip"
					class:on={opFilter === ''}
					aria-pressed={opFilter === ''}
					onclick={() => (opFilter = '')}>all {rows.length}</button
				>
				{#each opCounts as [op, n] (op)}
					<button
						class="fchip"
						class:on={opFilter === op}
						aria-pressed={opFilter === op}
						onclick={() => (opFilter = opFilter === op ? '' : op)}>{op} {n}</button
					>
				{/each}
			</div>
			<label class="toggle">
				<input type="checkbox" bind:checked={schemaOnly} />
				schema changes only
			</label>
		{/if}
		<input
			class="mono search"
			bind:value={search}
			placeholder="search predicate, author…"
			aria-label="Search the log"
		/>
		<Select bind:value={limit} ariaLabel="How many commits to read" options={LIMITS} />
		<button class="btn tiny ghost" onclick={() => loadHistory()} disabled={loading}>
			<RefreshCw size={11} />
			{loading ? '…' : 'reload'}
		</button>
	</div>

	<div data-testid="commit-log">
		<DataTable
			table={logTable}
			loading={loading && history === null && manifests.length === 0}
			emptyMessage={rows.length === 0
	? 'No versions — this table has no manifest the catalog can read.'
	: 'No commits match this filter.'}
			onrowclick={(row) => select(row.version)}
		>
			{#snippet footer()}
				<span class="mut"
					>{filtered.length} of {rows.length} commit{rows.length === 1 ? '' : 's'} shown{history !== null && history.length === Number(limit)
						? ` · reading the newest ${limit} — raise the limit to read further back`
						: ''}</span
				>
			{/snippet}
		</DataTable>
	</div>
	{#if restoreNote}
		<!-- The advisory outlives the panel: a successful restore closes the selection, and "this write was
		     NOT checked against the current tip" is exactly the sentence that must not vanish with it. -->
		<p class="mut">{restoreNote}</p>
	{/if}

	{#if unattributed > 0}
		<p class="mut legend">
			<strong>—</strong> in <em>by whom</em> means no governed run recorded that version ({unattributed}
			of {rows.length}): garbage collection and compaction write versions without one (the compaction
			service records an empty author), and a write that bypassed the catalog has no author to record.
			It is never a guess.
		</p>
	{/if}
	{#if producers === null}
		<p class="mut legend">
			The lineage store did not answer, so no author could be joined for any version — this is a
			missing answer, not an absence of authors.
		</p>
	{/if}

	{#if selected}
		<!-- The selected commit: its raw transaction fields (uninterpreted), the run that wrote it, and the
		     actions that act on THIS version. -->
		<div class="panel" data-testid="commit-panel">
			<div class="phead">
				<span class="mono strong">v{selected.version}</span>
				<span class="mut">{selected.operation ?? 'operation not recorded'}</span>
				<button class="btn tiny ghost" onclick={() => select(selected.version)}>close</button>
			</div>

			<dl class="kv">
				<dt>committed</dt>
				<dd class="mono">
					{selected.committed != null ? fmtEpoch(selected.committed, 'ms') : '—'}
					{#if selected.naive}
						<span class="mut">· wire value <code>{selected.naive}</code> (no offset)</span>
					{/if}
				</dd>
				<dt>manifest</dt>
				<dd class="mono">
					{fmtBytes(selected.manifestSize)}{selected.eTag ? ` · etag ${selected.eTag}` : ''}
				</dd>
				{#if selected.who}
					<dt>run</dt>
					<dd class="mono">
						{selected.who.author ?? '—'}
						{#if selected.who.operation}<span class="mut"> · {selected.who.operation}</span>{/if}
						{#if selected.who.runId}<span class="mut"> · {selected.who.runId}</span>{/if}
						{#if selected.who.eventTime}<span class="mut">
								· {fmtInstant(selected.who.eventTime)}</span>{/if}
					</dd>
					{#if selected.who.others.length > 0}
						<dt>other runs</dt>
						<dd class="mono">
							{#each selected.who.others as run (run.run_id)}
								<div>
									{run.run_id} · {run.event_type ?? '—'} · {fmtInstant(run.event_time)} · {run.author || '—'}
								</div>
							{/each}
						</dd>
					{/if}
				{/if}
			</dl>

			<h3>Raw transaction fields</h3>
			{#if selected.txn === null}
				<p class="mut">
					Not available — the commit log endpoint did not answer, so this row is the manifest entry only.
				</p>
			{:else if rawFields(selected.txn).length === 0}
				<p class="mut">The transaction carried no detail fields beyond its operation.</p>
			{:else}
				<table class="raw">
					<tbody>
						{#each rawFields(selected.txn) as field (field.key)}
							<tr><td class="mono k">{field.key}</td><td class="mono">{field.value}</td></tr>
						{/each}
					</tbody>
				</table>
			{/if}

			<h3>Actions</h3>
			<div class="acts">
				<div class="act">
					<input
						class="mono"
						bind:value={tagName}
						placeholder="tag name (e.g. blessed)"
						aria-label="Tag name"
					/>
					<button
						class="btn tiny"
						disabled={tagBusy || !tagName.trim()}
						onclick={() => runTag(selected.version)}
					>
						{tagBusy ? '…' : `Tag v${selected.version}`}
					</button>
				</div>
				<div class="act">
					<button class="btn tiny ghost" onclick={() => (compareBase = String(selected.version))}>
						Compare from v{selected.version}
					</button>
				</div>
				<div class="act">
					{#if restoreArmed === selected.version}
						<span class="mut">type <code>v{selected.version}</code> to confirm</span>
						<input
							class="mono"
							bind:value={restoreTyped}
							aria-label="Type the version to confirm the restore"
							placeholder="v{selected.version}"
						/>
						<button
							class="btn tiny danger"
							disabled={restoreBusy || !restoreTypedOk}
							onclick={() => runRestore(selected.version)}
						>
							{restoreBusy ? '…' : 'Confirm restore'}
						</button>
						<button
							class="btn tiny ghost"
							onclick={() => {
	restoreArmed = null;
	restoreTyped = '';
}}>cancel</button
						>
					{:else}
						<button
							class="btn tiny ghost"
							onclick={() => {
	restoreArmed = selected.version;
	restoreTyped = '';
	restoreError = null;
	restoreNote = null;
}}
						>
							Restore v{selected.version} (creates a new version)
						</button>
					{/if}
				</div>
			</div>
			{#if tagError}<p class="error">{tagError}</p>{/if}
			{#if restoreError}<p class="error">{restoreError}</p>{/if}
		</div>
	{/if}

	{#if attempted.length > 0}
		<!-- A FAIL/ABORT run carries no version (it produced no data), so it can never be a row in the log.
		     It is still part of the record and belongs beside it, not interleaved into it. -->
		<h3>Attempted writes that produced no version</h3>
		<table class="raw">
			<tbody>
				{#each attempted as run (run.run_id)}
					<tr>
						<td class="mono">{run.event_type}</td>
						<td class="mono">{fmtInstant(run.event_time)}</td>
						<td class="mono">{run.author || '—'}</td>
						<td>{run.error_message ?? ''}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<h3>Compare two versions</h3>
	<VersionCompare
		{table}
		{producers}
		bind:base={compareBase}
		versions={rows.map((r) => r.version)}
	/>

	<h3>Branches</h3>
	<div class="refs">
		<span class="chip mono">main</span>
		{#each branches as [name, b] (name)}
			<span class="chip branch mono"
				>{name}<span class="mut"> · {fmtBytes(b.manifestSize)}</span>
				{#if refConfirm?.kind === 'branch' && refConfirm.name === name}
					<button
						class="chip-x danger"
						aria-label="confirm delete branch {name}"
						disabled={refBusy}
						onclick={() => refDo(() => deleteTableBranch(table, name))}>delete?</button
					>
					<button class="chip-x" aria-label="cancel" onclick={() => (refConfirm = null)}>×</button>
				{:else}
					<button
						class="chip-x"
						title="delete branch"
						aria-label="delete branch {name}"
						disabled={refBusy}
						onclick={() => (refConfirm = { kind: 'branch', name })}>×</button
					>
				{/if}
			</span>
		{/each}
	</div>
	<form
		class="row"
		onsubmit={(e) => {
	e.preventDefault();
	runCreateBranch();
}}
	>
		<input
			class="mono"
			bind:value={newBranch}
			placeholder="new branch"
			aria-label="New branch name"
		/>
		<Select
			bind:value={newBranchFrom}
			ariaLabel="Branch from version"
			placeholder="from version (latest)"
			options={versionOptions}
		/>
		<button class="btn" type="submit" disabled={refBusy || !newBranch.trim()}>Create branch</button>
	</form>

	<h3>Tags</h3>
	<div class="refs">
		{#if tags.length === 0}
			<span class="mut">No tags — a promotion pins its version with one (e.g. blessed).</span>
		{:else}
			{#each tags as [name, t] (name)}
				{#if movingTag === name}
					<span class="chip tag mono">
						{name} →
						<span class="movesel">
							<Select
								bind:value={moveTo}
								ariaLabel="move {name} to version"
								placeholder="version"
								options={versionOptions}
							/>
						</span>
						<button class="chip-x" disabled={refBusy || !moveTo} onclick={runMoveTag}>save</button>
						<button class="chip-x" aria-label="cancel move" onclick={() => (movingTag = null)}>×</button>
					</span>
				{:else}
					<span class="chip tag mono"
						>{name} → v{t.version}
						<button
							class="chip-x"
							title="move tag"
							aria-label="move tag {name}"
							disabled={refBusy}
							onclick={() => {
	movingTag = name;
	moveTo = '';
}}>↪</button
						>
						{#if refConfirm?.kind === 'tag' && refConfirm.name === name}
							<button
								class="chip-x danger"
								aria-label="confirm delete tag {name}"
								disabled={refBusy}
								onclick={() => refDo(() => deleteTableTag(table, name))}>delete?</button
							>
							<button class="chip-x" aria-label="cancel" onclick={() => (refConfirm = null)}>×</button>
						{:else}
							<button
								class="chip-x"
								title="delete tag"
								aria-label="delete tag {name}"
								disabled={refBusy}
								onclick={() => (refConfirm = { kind: 'tag', name })}>×</button
							>
						{/if}
					</span>
				{/if}
			{/each}
		{/if}
	</div>
	{#if refError}<p class="error">{refError}</p>{/if}
</section>

<style>
	.hist {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	h3 {
		font-size: 11px;
		font-weight: 600;
		margin: 16px 0 6px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mut {
		color: var(--mut);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.legend {
		margin-top: 8px;
	}
	.banner {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		font-size: 12px;
		padding: 6px 10px;
		margin-bottom: 10px;
	}
	.banner.warn {
		border-color: color-mix(in oklab, var(--warn) 45%, transparent);
		background: color-mix(in oklab, var(--warn) 12%, transparent);
	}
	.banner.fail {
		border-color: color-mix(in oklab, var(--fail) 45%, transparent);
		background: color-mix(in oklab, var(--fail) 12%, transparent);
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
	.fchip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		color: var(--mut);
		cursor: pointer;
		font-size: 11px;
		padding: 2px 8px;
	}
	.fchip.on {
		border-color: color-mix(in oklab, var(--ink) 40%, transparent);
		color: var(--ink);
	}
	.toggle {
		align-items: center;
		color: var(--mut);
		display: flex;
		font-size: 12px;
		gap: 4px;
	}
	.search {
		flex: 1 1 160px;
		min-width: 120px;
	}
	input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
	}
	input[type='checkbox'] {
		padding: 0;
	}
	.btn {
		align-items: center;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		cursor: pointer;
		display: inline-flex;
		font-size: 12px;
		gap: 4px;
		padding: 4px 12px;
	}
	.btn.tiny {
		font-size: 11px;
		padding: 2px 8px;
	}
	.btn.ghost {
		background: transparent;
	}
	.chev {
		padding: 2px 4px;
	}
	.btn.danger {
		border-color: color-mix(in oklab, var(--fail) 50%, transparent);
		color: var(--fail);
	}
	.btn:disabled {
		cursor: default;
		opacity: 0.5;
	}
	.vcell {
		align-items: center;
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.v {
		font-weight: 600;
	}
	.tip,
	.tagchip {
		border: 1px solid var(--line);
		border-radius: 999px;
		font-size: 10px;
		padding: 0 5px;
	}
	.tip {
		background: color-mix(in oklab, var(--ok) 15%, transparent);
		border-color: color-mix(in oklab, var(--ok) 45%, transparent);
	}
	.tagchip {
		background: color-mix(in oklab, var(--amber) 15%, transparent);
		border-color: color-mix(in oklab, var(--amber) 45%, transparent);
	}
	.schemadot {
		color: var(--warn);
		font-size: 10px;
	}
	.bits,
	.who,
	.size {
		font-size: 12px;
	}
	/* The log stays scannable only if no single cell can stretch the table: an opaque subject, a long
	   predicate and a long index name are all unbroken strings. Each is capped and ellipsised, with the
	   full text in the cell's title (and, uncut, in the commit panel below). */
	.who {
		display: inline-flex;
		align-items: baseline;
		gap: 4px;
		max-width: 17ch;
	}
	.who > span:first-child {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.bits {
		display: inline-block;
		max-width: 20ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		vertical-align: bottom;
	}
	.sep {
		color: var(--faint);
		padding: 0 3px;
	}
	.via {
		color: var(--mut);
		font-size: 11px;
	}
	.qual {
		border: 1px solid color-mix(in oklab, var(--ok) 45%, transparent);
		border-radius: 999px;
		color: var(--ok);
		font-size: 10px;
		padding: 0 5px;
	}
	.qual.bad {
		border-color: color-mix(in oklab, var(--fail) 45%, transparent);
		color: var(--fail);
	}
	.naive {
		border-bottom: 1px dotted var(--faint);
	}
	.when {
		font-size: 11px;
	}
	.panel {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		margin-top: 12px;
		padding: 12px;
	}
	.phead {
		align-items: center;
		display: flex;
		gap: 8px;
		margin-bottom: 8px;
	}
	.strong {
		font-weight: 600;
	}
	.kv {
		display: grid;
		gap: 2px 12px;
		grid-template-columns: max-content 1fr;
		margin: 0;
	}
	.kv dt {
		color: var(--mut);
		font-size: 11px;
	}
	.kv dd {
		font-size: 12px;
		margin: 0;
		overflow-wrap: anywhere;
	}
	table.raw {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	table.raw td {
		border-bottom: 1px solid var(--line);
		padding: 3px 8px 3px 0;
		overflow-wrap: anywhere;
	}
	table.raw td.k {
		color: var(--mut);
		width: 40%;
	}
	.acts {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.act {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.refs {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-bottom: 8px;
	}
	.row {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 8px;
	}
	.chip {
		align-items: center;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		display: inline-flex;
		font-size: 12px;
		gap: 4px;
		padding: 1px 4px 1px 8px;
	}
	.chip-x {
		background: transparent;
		border: none;
		color: var(--faint);
		cursor: pointer;
		font-size: 12px;
		padding: 0 4px;
	}
	.chip-x:hover {
		color: var(--fail);
	}
	.chip-x.danger {
		color: var(--fail);
	}
	.movesel {
		display: inline-flex;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
		margin-top: 8px;
	}
	.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
	code {
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 11px;
	}
</style>
