<script lang="ts">
	// A declared table: its name is reserved and it holds no version, so there is no schema, row,
	// history or index to show, and the doors that read a dataset answer it 404. The one thing to do
	// here is its first write, which the catalog's create door lands into the declared location
	// (`POST /v1/table/{id}/create` with an Arrow body); appending is refused until then.
	import { Button } from '@rask/ui/button';
	import { createTableWithRows } from '../catalog';
	import { parseJsonRows, rowsToArrow } from '../json-rows';

	let { table, onwritten }: { table: string; onwritten: () => Promise<void> } = $props();

	let rowsJson = $state('');
	let busy = $state(false);
	let problem = $state<string | null>(null);

	async function write(): Promise<void> {
		if (busy || !rowsJson.trim()) return;
		problem = null;
		const parsed = parseJsonRows(rowsJson);
		if (!parsed.ok) {
			problem = `Invalid rows: ${parsed.reason}`;
			return;
		}
		let arrow: Uint8Array;
		try {
			arrow = rowsToArrow(parsed.rows);
		} catch (e) {
			problem = `Encode failed: ${e instanceof Error ? e.message : String(e)}`;
			return;
		}
		busy = true;
		try {
			const res = await createTableWithRows({ table, arrow });
			if (res.ok) {
				rowsJson = '';
				await onwritten();
			} else if (res.status === 403) {
				problem = 'Denied: the first write needs create access (can_create_table) here.';
			} else {
				problem = res.detail;
			}
		} finally {
			busy = false;
		}
	}
</script>

<section class="declared" data-testid="declared-table">
	<h2>Declared, waiting for its first write</h2>
	<p>
		This table's name is reserved and it holds no data yet: no version, no schema, no rows. Its
		first write fixes its schema. Write it here, or through the namespace doors from any client.
	</p>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			void write();
		}}
	>
		<label for="first-rows">First rows, as a JSON array of objects</label>
		<textarea
			id="first-rows"
			class="mono"
			bind:value={rowsJson}
			placeholder={'[{ "id": 1, "name": "a" }]'}></textarea>
		<div class="actions">
			<Button type="submit" size="sm" disabled={busy || !rowsJson.trim()}>
				{busy ? 'Writing…' : 'Write first rows'}
			</Button>
			{#if !rowsJson.trim()}<span class="hint">Enter at least one row to write.</span>{/if}
		</div>
		{#if problem}<p class="problem" role="alert">{problem}</p>{/if}
	</form>
</section>

<style>
	.declared {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 16px 18px;
		margin: 8px 0 24px;
		font-size: 13px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 6px;
	}
	p {
		margin: 0 0 12px;
		color: var(--muted-foreground);
		max-width: 72ch;
	}
	form {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	label {
		font-size: 12px;
		color: var(--muted-foreground);
	}
	textarea {
		min-height: 90px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--foreground);
		font-size: 12px;
		padding: 6px 8px;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.hint {
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.problem {
		color: var(--destructive);
		font-size: 12px;
		margin: 0;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
