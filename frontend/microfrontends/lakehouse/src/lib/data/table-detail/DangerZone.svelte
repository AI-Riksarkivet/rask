<script lang="ts">
	// #85 danger zone — rename (navigates to the renamed id), drop + deregister behind an
	// AlertDialog confirm (NamespaceRegistry's pattern, incl. the always-close-in-finally fix).
	// The dialog travels WITH this section: its open/action state is this workflow's own.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — an armed confirm dies with it. No `onchanged` prop:
	// every success here leaves the page (the id no longer names this table).
	import { AlertDialog } from '@rask/ui/alert-dialog';
	import { Trash2 } from '@lucide/svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { deregisterTable, dropTable, renameTable } from '../remote/catalog.remote';

	let { table }: { table: string } = $props();

	let renameTableTo = $state('');
	let dangerBusy = $state(false);
	let dangerError = $state<string | null>(null);
	let dangerOpen = $state(false);
	let dangerAction = $state<'drop' | 'deregister' | null>(null);
	// The `<ns>$` prefix the renamed table keeps — this form renames within the table's own namespace.
	const nsPrefix = $derived(table.includes('$') ? table.slice(0, table.lastIndexOf('$') + 1) : '');

	function openDanger(action: 'drop' | 'deregister'): void {
		dangerAction = action;
		dangerError = null;
		dangerOpen = true;
	}

	function dangerFail(action: string, status: number, detail: string): void {
		if (status === 401) dangerError = `Sign in — ${action} is a per-user action.`;
		else if (status === 403)
			dangerError =
				action === 'deregister'
					? 'Denied: deregistering needs the owner rung (can_deregister).'
					: `Denied: ${action} needs the owner rung (can_drop).`;
		else if (status === 0) dangerError = `Catalog unreachable — the ${action} was not applied.`;
		else dangerError = detail;
	}

	async function confirmDanger(): Promise<void> {
		const action = dangerAction;
		if (action === null || dangerBusy) return;
		dangerBusy = true;
		dangerError = null;
		try {
			const res = action === 'drop' ? await dropTable({ table }) : await deregisterTable({ table });
			if (res.ok) {
				await goto(`${base}/catalog/tables`); // the id no longer names a table — back to the registry
			} else {
				dangerFail(action, res.status, res.detail);
			}
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			dangerError = `${action} response drifted from the contract: ${String(err)}`;
		} finally {
			// ALWAYS close + disarm: bits-ui's AlertDialog.Action does not auto-close, so leaving the dialog
			// open would keep the destructive action armed for a second, confirm-free fire (audit: major).
			dangerBusy = false;
			dangerOpen = false;
			dangerAction = null;
		}
	}

	async function runRenameTable(): Promise<void> {
		const to = renameTableTo.trim();
		if (dangerBusy || !to) return;
		dangerBusy = true;
		dangerError = null;
		const newId = `${nsPrefix}${to}`;
		try {
			const res = await renameTable({ table, newName: to });
			if (res.ok) {
				renameTableTo = '';
				// the old id no longer exists — follow the table to its renamed detail page
				await goto(`${base}/catalog/tables/${encodeURIComponent(newId)}`);
			} else if (res.status === 409) {
				dangerError = `Denied: a table named ${newId} already exists.`;
			} else {
				dangerFail('rename', res.status, res.detail);
			}
		} catch (err) {
			dangerError = `rename response drifted from the contract: ${String(err)}`;
		} finally {
			dangerBusy = false;
		}
	}
</script>

<section class="dangerzone">
	<h2>Danger zone</h2>
	<form
		class="row"
		onsubmit={(e) => {
	e.preventDefault();
	runRenameTable();
}}
	>
		<input
			class="mono"
			bind:value={renameTableTo}
			placeholder="new table name"
			aria-label="Rename table to"
		/>
		<button class="btn" type="submit" disabled={dangerBusy || !renameTableTo.trim()}> Rename </button>
	</form>
	<p class="mut">
		Rename relocates the table within its namespace and navigates to the new id (owner-gated: can_drop
		on the source + can_create_table on the destination).
	</p>
	<div class="row">
		<button class="btn danger" disabled={dangerBusy} onclick={() => openDanger('deregister')}>
			Deregister
		</button>
		<button class="btn danger" disabled={dangerBusy} onclick={() => openDanger('drop')}>
			<Trash2 size={12} /> Drop table
		</button>
	</div>
	<p class="mut">
		Deregister detaches the table from the catalog (data stays on storage); drop deletes it
		permanently.
	</p>
	{#if dangerError}<p class="error">{dangerError}</p>{/if}
</section>

<AlertDialog.Root bind:open={dangerOpen}>
	<AlertDialog.Content>
		<AlertDialog.Title>
			{dangerAction === 'deregister' ? 'Deregister' : 'Drop'} table {table}
		</AlertDialog.Title>
		<AlertDialog.Description>
			{#if dangerAction === 'deregister'}
				This detaches <span class="mono">{table}</span> from the catalog (owner-gated: can_deregister). The
				data stays on storage, but the catalog forgets the id and its grants are revoked.
			{:else}
				This permanently drops <span class="mono">{table}</span> and its data (owner-gated: can_drop). Every
				version, tag and branch is deleted; its grants are revoked.
			{/if}
		</AlertDialog.Description>
		<div class="dialog-actions">
			<AlertDialog.Cancel disabled={dangerBusy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="border-destructive/40 bg-destructive/15 text-destructive hover:bg-destructive/25"
				disabled={dangerBusy}
				onclick={confirmDanger}
			>
				{dangerAction === 'deregister' ? 'Deregister' : 'Drop'}
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>

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
	.error {
		color: var(--fail);
		font-size: 12px;
	}
	.row {
		display: flex;
		gap: 8px;
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
	.btn.danger {
		color: var(--fail);
	}
	.dangerzone {
		border-top: 1px solid color-mix(in srgb, var(--fail) 30%, var(--line));
		padding-top: 12px;
	}
	.dangerzone form {
		margin-bottom: 4px;
	}
	.dangerzone .row {
		margin: 8px 0 4px;
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
</style>
