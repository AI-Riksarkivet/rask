<script lang="ts">
	// #113 compare two versions of a table — the "what is different between then and now" half of a commit
	// log, which the log's per-row summary cannot answer (a row says what ONE commit did).
	//
	// Two sources, each authoritative: the measured output statistics of the run that wrote each version
	// (lineage `row_count` / `size_bytes`) and the per-version column schema the same run recorded
	// (`GET /datasets/{name}/schema?version=N`). The schema read is a server-side per-version query, so it
	// survives the GC that expires a manifest — the reference console reads its schemas out of the table
	// metadata and silently blanks once they expire.
	//
	// Two deliberate honesty rules: a tile is OMITTED when either side reported nothing (never a 0 that
	// reads as "shrank to nothing"), and the schema join is by field NAME because the recorded schema has no
	// field ids — so a rename shows as a remove plus an add, and the view says exactly that instead of
	// guessing.
	import { Select } from '@rask/ui/select';
	import { fetchSchema } from '$lib/api';
	import type { ProducerInfo, SchemaField } from '@rask/api/lineage';
	import { authorFor, delta, diffSchemas, fmtBytes, type SchemaDiffRow } from './history';

	let {
		table,
		versions,
		producers,
		base = $bindable(''),
	}: {
		table: string;
		/** The versions the FORMAT reports, newest first — the only versions that exist. */
		versions: number[];
		producers: ProducerInfo[] | null;
		/** The base version, bindable so a row action ("compare from v7") can set it. */
		base?: string;
	} = $props();

	let compareValue = $state('');

	const baseVersion = $derived(base ? Number(base) : null);
	const compareVersion = $derived(compareValue ? Number(compareValue) : null);

	const ascending = $derived([...versions].sort((a, b) => a - b));
	const baseOptions = $derived(
		// Everything except the newest: a base with nothing after it has nothing to compare to.
		ascending.slice(0, -1).map((v) => ({ value: String(v), label: `v${v}` })),
	);
	// Derived from the base, so "compare" can only ever be NEWER — the direction cannot be wrong, which is
	// the one thing the reference console's compare dialog gets unambiguously right.
	const compareOptions = $derived(
		ascending
			.filter((v) => baseVersion != null && v > baseVersion)
			.map((v) => ({
				value: String(v),
				label: `v${v}`,
			})),
	);

	type Side = { version: number; fields: SchemaField[] | null };
	let baseSide = $state<Side | null>(null);
	let compareSide = $state<Side | null>(null);
	let loading = $state(false);
	// Monotonic request token: the schema pair is keyed by (table, base, compare) and the user can change
	// any of the three mid-flight. Checked in the success path so a slow earlier pair can never paint over a
	// newer one.
	let token = 0;

	$effect(() => {
		const forTable = table;
		const b = baseVersion;
		const c = compareVersion;
		const mine = ++token;
		if (b == null || c == null) {
			baseSide = null;
			compareSide = null;
			loading = false;
			return;
		}
		loading = true;
		void Promise.all([fetchSchema(forTable, b), fetchSchema(forTable, c)]).then(([left, right]) => {
			if (mine !== token) return;
			baseSide = { version: b, fields: left?.fields ?? null };
			compareSide = { version: c, fields: right?.fields ?? null };
			loading = false;
		});
	});

	const baseRun = $derived(baseVersion == null ? null : authorFor(baseVersion, producers));
	const compareRun = $derived(compareVersion == null ? null : authorFor(compareVersion, producers));
	const rowDelta = $derived(delta(baseRun?.rowCount ?? null, compareRun?.rowCount ?? null));
	const byteDelta = $derived(delta(baseRun?.sizeBytes ?? null, compareRun?.sizeBytes ?? null));

	const schemaRows = $derived<SchemaDiffRow[]>(
		baseSide?.fields || compareSide?.fields
			? diffSchemas(baseSide?.fields, compareSide?.fields)
			: [],
	);
	const changed = $derived(schemaRows.filter((r) => r.status !== 'unchanged'));
	const signed = (n: number) => (n > 0 ? `+${n}` : String(n));
</script>

<div class="cmp">
	<div class="pickers">
		<label>
			<span class="lbl">Base (older)</span>
			<Select
				bind:value={base}
				ariaLabel="Base version"
				placeholder="version…"
				options={baseOptions}
			/>
		</label>
		<label>
			<span class="lbl">Compare (newer)</span>
			<Select
				bind:value={compareValue}
				ariaLabel="Compare version"
				placeholder={baseVersion == null ? 'pick a base first' : 'version…'}
				options={compareOptions}
				disabled={baseVersion == null}
			/>
		</label>
	</div>

	{#if baseVersion == null}
		<p class="mut">Pick a base version to compare from.</p>
	{:else if compareVersion == null}
		<p class="mut">Pick a newer version to compare v{baseVersion} against.</p>
	{:else if loading}
		<p class="mut">Reading both schemas…</p>
	{:else}
		<p class="mut">v{baseVersion} → v{compareVersion}</p>
		<div class="tiles">
			<!-- A tile only exists when BOTH sides measured the metric. "0 rows" and "we were not told"
			     are different facts and this view never conflates them. -->
			{#if rowDelta.delta !== null}
				<div class="tile" data-metric="rows">
					<span class="tval mono">{signed(rowDelta.delta)}</span>
					<span class="tlbl">rows · {rowDelta.base} → {rowDelta.compare}</span>
				</div>
			{/if}
			{#if byteDelta.delta !== null}
				<div class="tile" data-metric="bytes">
					<span class="tval mono">{signed(byteDelta.delta)} B</span>
					<span class="tlbl">size · {fmtBytes(byteDelta.base)} → {fmtBytes(byteDelta.compare)}</span>
				</div>
			{/if}
			{#if rowDelta.delta === null && byteDelta.delta === null}
				<p class="mut">
					No measured statistics on either side — the runs that wrote these versions recorded no row
					count or size (a client-direct write, or maintenance).
				</p>
			{/if}
		</div>

		<h4>Schema</h4>
		{#if baseSide?.fields == null && compareSide?.fields == null}
			<p class="mut">
				The lineage store has no recorded schema for v{baseVersion} or v{compareVersion} — only versions written
				through a governed run carry one.
			</p>
		{:else if changed.length === 0}
			<p class="mut">Identical columns — {schemaRows.length} unchanged.</p>
		{:else}
			<table>
				<thead><tr><th>column</th><th>change</th><th>type</th></tr></thead>
				<tbody>
					{#each schemaRows as row (row.name)}
						<tr class:dim={row.status === 'unchanged'} data-status={row.status}>
							<td class="mono">{row.name}</td>
							<td class="st {row.status}">{row.status}</td>
							<td class="mono">
								{#if row.status === 'retyped'}
									<span class="was">{row.oldType}</span> → {row.type}
								{:else}
									{row.type ?? row.oldType ?? '—'}
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
			<p class="mut fine">
				Columns are matched by name: the recorded schema carries no field ids, so a renamed column reads
				as one removed and one added, not as a rename.
			</p>
		{/if}
	{/if}
</div>

<style>
	.cmp {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 12px;
		margin-top: 12px;
	}
	.pickers {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		align-items: end;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.lbl {
		color: var(--mut);
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mut {
		color: var(--mut);
		font-size: 12px;
		margin: 10px 0 0;
	}
	.fine {
		font-size: 11px;
	}
	.tiles {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 10px;
	}
	.tile {
		display: flex;
		flex-direction: column;
		gap: 2px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel-2);
		padding: 6px 10px;
	}
	.tval {
		font-size: 15px;
		font-weight: 600;
	}
	.tlbl {
		color: var(--mut);
		font-size: 11px;
	}
	h4 {
		font-size: 11px;
		font-weight: 600;
		margin: 14px 0 4px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 12px;
	}
	th {
		color: var(--mut);
		font-weight: 500;
		text-align: left;
		padding: 4px 8px 4px 0;
		border-bottom: 1px solid var(--line);
	}
	td {
		padding: 4px 8px 4px 0;
		border-bottom: 1px solid var(--line);
	}
	tr.dim td {
		opacity: 0.55;
	}
	.st.added {
		color: var(--ok);
	}
	.st.removed {
		color: var(--fail);
	}
	.st.retyped {
		color: var(--warn);
	}
	.was {
		color: var(--mut);
		text-decoration: line-through;
	}
	.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
</style>
