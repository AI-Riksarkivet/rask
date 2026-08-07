<script lang="ts">
	// #75 recovery — shown on the table-detail 404, exactly where a dropped table's owner lands.
	// `recoverable` is the catalog's own trash record: if one exists the bytes are still there and
	// the deadline is real; if not (or while the lookup is in flight), the drop was destructive and
	// THIS card renders the plain not-registered copy in its own {:else} branch below.
	//
	// Split out of TableDetail.svelte (#98). The parent renders it under the 404 branch and passes
	// `onrecovered` — the undrop bumps the registry, so the parent re-reads the detail.
	import { Undo2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { Subject } from '@rask/ui/identity';
	import { fetchTableTasks, undropTable, type TrashEntry } from '../remote/catalog.remote';

	let { table, onrecovered }: { table: string; onrecovered: () => Promise<void> } = $props();

	let recoverable = $state<TrashEntry | null>(null);
	let recovering = $state(false);
	let recoverError = $state<string | null>(null);

	// Only asked for on the 404 (the parent's mount condition), so a live table never pays a
	// round-trip for a question that cannot apply to it. The parent keys the detail body by table,
	// so this instance never survives a navigation — no latest-wins guard needed.
	$effect(() => {
		void fetchTableTasks(table).then((res) => {
			recoverable = res.ok ? (res.data[0] ?? null) : null;
		});
	});

	async function recover(): Promise<void> {
		if (recovering) return;
		recovering = true;
		recoverError = null;
		try {
			const res = await undropTable(table);
			if (res.ok) {
				recoverable = null;
				await onrecovered();
				return;
			}
			// VERBATIM, like every other refusal in this estate — the catalog names the reason (an
			// expired grace period reads differently from a permission denial, and both matter here).
			recoverError = res.detail;
		} finally {
			recovering = false;
		}
	}
</script>

{#if recoverable}
	<div class="recover">
		<h2>This table was dropped — and is still recoverable</h2>
		<p>
			Its bytes were never deleted. Recovering re-registers them at
			<code class="mono">{recoverable.location}</code>.
		</p>
		<p class="mut">
			Dropped by <Subject value={recoverable.dropped_by} />
			on {recoverable.dropped_at.slice(0, 10)} · recoverable until
			<strong>{recoverable.expires_at.slice(0, 10)}</strong>, after which the maintenance sweep reports
			it for reclamation.
		</p>
		{#if recoverError}<p class="problem" data-testid="undrop-problem">{recoverError}</p>{/if}
		<Button size="sm" disabled={recovering} onclick={recover}>
			<Undo2 size={14} />
			{recovering ? 'Recovering…' : 'Undrop this table'}
		</Button>
	</div>
{:else}
	<div class="empty">
		<p>
			Not a catalog-registered table — storage-managed datasets (medallion zones) have no catalog
			detail. Its lineage is on the <a href="/lakehouse/lineage" data-sveltekit-reload>explorer</a>.
		</p>
	</div>
{/if}

<style>
	.recover {
		border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--line));
		background: color-mix(in srgb, var(--warn) 7%, transparent);
		border-radius: var(--radius-sm);
		padding: 16px 18px;
		margin: 24px 0;
	}
	/* The original's base h2 rule (uppercase/tracked/faint) merged with its .recover override
	   (15px) — the review caught this card as the one child that dropped the base half. */
	.recover h2 {
		margin: 0 0 8px;
		font-size: 15px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
	}
	.recover p {
		margin: 0 0 8px;
		font-size: 13px;
	}
	.recover .problem {
		color: var(--fail);
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
</style>
