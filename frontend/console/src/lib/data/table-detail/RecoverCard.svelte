<script lang="ts">
	// #75 recovery — shown on the table-detail 404, exactly where a dropped table's owner lands.
	// `recoverable` is the catalog's own trash record: if one exists the bytes are still there and
	// the deadline is real.
	//
	// THE PROBE IS ITS OWN STATE, not an absent record (#147): while the lookup is in flight, or after
	// it failed, the card must not say the data is gone. The catalog's trash door
	// (`GET /management/v1/table/{id}/tasks`) answers 404 when there is no trash record the caller
	// may see, a definite answer that discloses nothing, and the not-registered copy renders for it.
	// An unreachable catalog (status 0), a 5xx or any other refusal is a read failure and reads as one.
	//
	// Split out of TableDetail.svelte (#98). The parent renders it under the 404 branch and passes
	// `onrecovered` — the undrop bumps the registry, so the parent re-reads the detail.
	import { Undo2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { formatZoned } from '@riksarkivet/ui/utils';
	import { Subject } from '#lib/access/identity/index.ts';
	import { fetchTableTasks, undropTable, type TrashEntry } from '../remote/catalog.api';

	let { table, onrecovered }: { table: string; onrecovered: () => Promise<void> } = $props();

	let recovering = $state(false);
	let recoverError = $state<string | null>(null);
	// A successful undrop leaves no record to offer while the parent re-reads the detail.
	let recovered = $state(false);

	// Only asked for on the 404 (the parent's mount condition), so a live table never pays a
	// round-trip for a question that cannot apply to it. The parent keys this card by table, and the
	// read is held per table, so one table's record never shows on another's card.
	const tasks = $derived(fetchTableTasks(table));
	// Deliberately NOT a `'loading'` sentinel inside the `TrashEntry | null` union: that string is
	// TRUTHY, so the recover branch would render for it and read fields a string does not have. A read
	// that rejected (a dropped connection, a page-level failure) is a read failure, never "Checking…"
	// for ever.
	const probe = $derived.by(
		():
			| { state: 'loading' }
			| { state: 'done'; entry: TrashEntry | null }
			| { state: 'failed'; detail: string } => {
			if (tasks.error !== undefined) return { state: 'failed', detail: String(tasks.error) };
			const res = tasks.current;
			if (res === undefined) return { state: 'loading' };
			if (res.ok) return { state: 'done', entry: res.data[0] ?? null };
			if (res.status === 404) return { state: 'done', entry: null };
			return { state: 'failed', detail: res.detail };
		},
	);
	const recoverable = $derived(probe.state === 'done' && !recovered ? probe.entry : null);

	async function recover(): Promise<void> {
		if (recovering) return;
		recovering = true;
		recoverError = null;
		try {
			const res = await undropTable({ table });
			if (res.ok) {
				recovered = true;
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

{#if probe.state === 'loading'}
	<div class="empty"><p>Checking whether this table is recoverable…</p></div>
{:else if probe.state === 'failed'}
	<div class="empty stack">
		<p class="problem">
			Could not read this table's trash record — recovery may still be possible. This is a read
			failure, not a verdict on your data.
		</p>
		<!-- VERBATIM, never a synthesised "HTTP {status}": `upstreamJSON` maps an unreachable catalog to
		     status 0, which would print the meaningless "HTTP 0". Same rule the undrop refusal follows. -->
		<p class="mut">{probe.detail}</p>
	</div>
{:else if recoverable}
	<div class="recover">
		<h2>This table was dropped — and is still recoverable</h2>
		<p>
			Its bytes were never deleted. Recovering re-registers them at
			<code class="mono">{recoverable.location}</code>.
		</p>
		<p class="mut">
			Dropped by <Subject value={recoverable.dropped_by} />
			on {formatZoned(recoverable.dropped_at)} · recoverable until
			<strong>{formatZoned(recoverable.expires_at)}</strong>, after which the maintenance sweep
			reports it for reclamation.
		</p>
		{#if recoverError}<p class="problem" data-testid="undrop-problem">{recoverError}</p>{/if}
		<Button size="sm" disabled={recovering} onclick={recover}>
			<Undo2 size={14} />
			{recovering ? 'Recovering…' : 'Undrop this table'}
		</Button>
	</div>
{:else}
	<div class="empty">
		<p>The catalog registers no table by this name, so there is no detail to show.</p>
	</div>
{/if}

<style>
	.recover {
		border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
		background: color-mix(in srgb, var(--warning) 7%, transparent);
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
		color: var(--muted-foreground);
	}
	.recover p {
		margin: 0 0 8px;
		font-size: 13px;
	}
	.recover .problem {
		color: var(--destructive);
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--muted-foreground);
		padding: 32px 0;
	}
	/* The read-failure branch stacks two lines (the plain-English cause, then the upstream's verbatim
	   detail); the base rule is a centred ROW, which would sit them side by side. */
	.empty.stack {
		flex-direction: column;
		align-items: flex-start;
		gap: 4px;
	}
	.empty .problem {
		color: var(--destructive);
		font-size: 13px;
	}
</style>
