<script lang="ts">
	// The commit log's selected commit (#113): its raw transaction fields, uninterpreted, and the two
	// actions that act on THIS version, tag it and restore it. Its drafts and the armed restore are its
	// own; the log mounts it keyed by version, so selecting another commit starts it afresh.
	//
	// Restore MINTS A FRESH VERSION pointing at the old data, so nothing is orphaned and no branch becomes
	// unreachable. It does move the table's tip, so it asks for the version to be typed, and it refuses
	// when the tip moved under the page.
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { fetchTableHistory } from './catalog';
	import { fmtBytes, fmtEpoch, rawFields, type LogRow } from './history';
	import { createTableTag, restoreTableVersion } from './remote/catalog.api';

	let {
		table,
		row,
		currentVersion,
		onclose,
		onrestored,
		onnote,
		onchange,
	}: {
		table: string;
		row: LogRow;
		/** The version the page believes is current, for the restore staleness check. */
		currentVersion: number | null;
		onclose: () => void;
		/** A restore landed: the log closes this panel and reads itself and the page again. */
		onrestored: () => Promise<void>;
		/** The restore's advisory, which the log keeps on screen after this panel closes. */
		onnote: (note: string | null) => void;
		/** A tag landed: the page reads its aggregate again. */
		onchange: () => void;
	} = $props();

	let restoreArmed = $state(false);
	let restoreTyped = $state('');
	let restoreBusy = $state(false);
	let restoreError = $state<string | null>(null);

	const restoreTypedOk = $derived(
		restoreArmed &&
			[String(row.version), `v${row.version}`].includes(restoreTyped.trim().toLowerCase()),
	);

	async function runRestore(): Promise<void> {
		if (restoreBusy) return;
		restoreBusy = true;
		restoreError = null;
		onnote(null);
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
				onnote(
					'The concurrent-write check could not run (the catalog did not serve the commit log), so this restore was not verified against the current tip.',
				);
			}
			const res = await restoreTableVersion({ table, version: row.version });
			if (res.ok) {
				restoreArmed = false;
				restoreTyped = '';
				await onrestored();
			} else if (res.status === 401) {
				restoreError = 'Sign in to restore a version.';
			} else if (res.status === 403) {
				restoreError = 'Denied: restoring a version needs the modifier rung (can_restore).';
			} else {
				restoreError = res.detail;
			}
		} finally {
			restoreBusy = false;
		}
	}

	// Tag THIS version (the pin-this affordance, instead of picking a version out of a dropdown).
	let tagName = $state('');
	let tagBusy = $state(false);
	let tagError = $state<string | null>(null);

	async function runTag(): Promise<void> {
		const name = tagName.trim();
		if (tagBusy || !name) return;
		tagBusy = true;
		tagError = null;
		try {
			const res = await createTableTag({ table, tag: name, version: row.version });
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
</script>

<div class="panel" data-testid="commit-panel">
	<div class="phead">
		<span class="mono strong">v{row.version}</span>
		<span class="mut">{row.operation ?? 'operation not recorded'}</span>
		<Button variant="ghost" size="xs" onclick={onclose}>close</Button>
	</div>

	<dl class="kv">
		<dt>committed</dt>
		<dd class="mono">
			{row.committed != null ? fmtEpoch(row.committed, 'ms') : '—'}
			{#if row.naive}
				<span class="mut">· wire value <code>{row.naive}</code> (no offset)</span>
			{/if}
		</dd>
		<dt>manifest</dt>
		<dd class="mono">
			{fmtBytes(row.manifestSize)}{row.eTag ? ` · etag ${row.eTag}` : ''}
		</dd>
	</dl>

	<h3>Raw transaction fields</h3>
	{#if row.txn === null}
		<p class="mut">
			Not available — the commit log endpoint did not answer, so this row is the manifest entry
			only.
		</p>
	{:else if rawFields(row.txn).length === 0}
		<p class="mut">The transaction carried no detail fields beyond its operation.</p>
	{:else}
		<table class="raw">
			<tbody>
				{#each rawFields(row.txn) as field (field.key)}
					<tr><td class="mono k">{field.key}</td><td class="mono">{field.value}</td></tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<h3>Actions</h3>
	<div class="acts">
		<div class="act">
			<Input
				class="h-7 w-56 font-mono text-xs"
				bind:value={tagName}
				placeholder="tag name (e.g. blessed)"
				aria-label="Tag name"
			/>
			<Button variant="outline" size="xs" disabled={tagBusy || !tagName.trim()} onclick={runTag}>
				{tagBusy ? '…' : `Tag v${row.version}`}
			</Button>
		</div>
		<div class="act">
			{#if restoreArmed}
				<span class="mut">type <code>v{row.version}</code> to confirm</span>
				<Input
					class="h-7 w-28 font-mono text-xs"
					bind:value={restoreTyped}
					aria-label="Type the version to confirm the restore"
					placeholder="v{row.version}"
				/>
				<Button
					variant="destructive"
					size="xs"
					disabled={restoreBusy || !restoreTypedOk}
					onclick={runRestore}
				>
					{restoreBusy ? '…' : 'Confirm restore'}
				</Button>
				<Button
					variant="ghost"
					size="xs"
					onclick={() => {
						restoreArmed = false;
						restoreTyped = '';
					}}>cancel</Button
				>
			{:else}
				<Button
					variant="ghost"
					size="xs"
					onclick={() => {
						restoreArmed = true;
						restoreTyped = '';
						restoreError = null;
						onnote(null);
					}}
				>
					Restore v{row.version} (creates a new version)
				</Button>
			{/if}
		</div>
	</div>
	{#if tagError}<p class="error">{tagError}</p>{/if}
	{#if restoreError}<p class="error">{restoreError}</p>{/if}
</div>

<style>
	h3 {
		font-size: 11px;
		font-weight: 600;
		margin: 16px 0 6px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.panel {
		border: 1px solid var(--border);
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
		color: var(--muted-foreground);
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
		border-bottom: 1px solid var(--border);
		padding: 3px 8px 3px 0;
		overflow-wrap: anywhere;
	}
	table.raw td.k {
		color: var(--muted-foreground);
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
	.error {
		color: var(--destructive);
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
