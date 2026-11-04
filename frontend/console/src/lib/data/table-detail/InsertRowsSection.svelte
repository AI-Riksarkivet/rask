<script lang="ts">
	// #64 data-plane row insert — append JSON rows, browser-encoded to Arrow via apache-arrow.
	// Writer-gated (can_write_data); the catalog's honest error surfaces on a schema mismatch.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — a draft typed on table A cannot land on B.
	import { tableFromJSON, tableToIPC } from 'apache-arrow';
	import { insertRows } from '../catalog';

	let { table, onchanged }: { table: string; onchanged: () => Promise<void> } = $props();

	let insertJson = $state('');
	let insertBusy = $state(false);
	let insertMsg = $state<{ ok: boolean; text: string } | null>(null);

	async function runInsert(): Promise<void> {
		if (insertBusy || !insertJson.trim()) return;
		insertBusy = true;
		insertMsg = null;
		let rows: Record<string, unknown>[];
		try {
			const parsed: unknown = JSON.parse(insertJson);
			if (!Array.isArray(parsed) || parsed.length === 0) {
				throw new Error('expected a non-empty JSON array of row objects');
			}
			rows = parsed as Record<string, unknown>[];
		} catch (e) {
			insertMsg = {
				ok: false,
				text: `Invalid rows: ${e instanceof Error ? e.message : String(e)}`,
			};
			insertBusy = false;
			return;
		}
		try {
			// Browser-side Arrow-IPC encode (apache-arrow) → the catalog's Arrow-body insert. The inferred
			// schema must match the table's — a mismatch is the catalog's honest error, surfaced below.
			const arrow = tableToIPC(tableFromJSON(rows), 'stream');
			const res = await insertRows(table, arrow);
			if (res.ok) {
				insertMsg = {
					ok: true,
					text: `Inserted ${rows.length} row${rows.length === 1 ? '' : 's'}.`,
				};
				insertJson = '';
				await onchanged(); // the insert bumped the version — refresh stats + versions
			} else if (res.status === 401) {
				insertMsg = { ok: false, text: 'Sign in to insert rows.' };
			} else if (res.status === 403) {
				insertMsg = {
					ok: false,
					text: 'Denied: inserting rows needs writer access (can_write_data).',
				};
			} else {
				insertMsg = { ok: false, text: res.detail };
			}
		} catch (e) {
			insertMsg = {
				ok: false,
				text: `Encode failed: ${e instanceof Error ? e.message : String(e)}`,
			};
		} finally {
			insertBusy = false;
		}
	}
</script>

<section>
	<h2>Insert rows</h2>
	<p class="mut">Append rows as a JSON array of objects whose keys match the schema.</p>
	<textarea class="mono ins" bind:value={insertJson} placeholder={'[{ "id": 1, "name": "a" }]'}
	></textarea>
	<div class="ins-row">
		<button class="btn" disabled={insertBusy || !insertJson.trim()} onclick={runInsert}>
			{insertBusy ? '…' : 'Insert'}
		</button>
		{#if insertMsg}<span class="ins-msg" class:okmsg={insertMsg.ok} class:error={!insertMsg.ok}
			>{insertMsg.text}</span
		>{/if}
	</div>
</section>

<style>
	section {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.ins {
		width: 100%;
		min-height: 72px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 7px 9px;
		resize: vertical;
	}
	.ins-row {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 6px;
	}
	.ins-msg {
		font-size: 12px;
	}
	.okmsg {
		color: var(--ok);
	}
	.error {
		color: var(--fail);
		font-size: 12px;
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
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
