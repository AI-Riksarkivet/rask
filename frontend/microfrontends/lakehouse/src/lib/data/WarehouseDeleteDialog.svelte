<script lang="ts">
	// The warehouse DESTROY door's confirmation (`DELETE /v1/warehouses/{id}`), and the shape of it is
	// the point: the catalog's three opt-ins never share a default, so neither does this dialog.
	//
	//   1. It opens with NOTHING selected and tries the plain delete — the recoverable one. A warehouse
	//      that still holds namespaces refuses 409 with a detail that names them, and that refusal is
	//      rendered verbatim, because it is written to be actionable and already names the fix.
	//   2. `cascade` is offered ONLY after such a refusal, and it lists exactly the namespaces the
	//      refusal named. Offering it up front would be asking for consent to drop things nobody has
	//      been shown.
	//   3. `purge_bucket` is the only irreversible step in the whole surface, so it costs the user the
	//      bucket's NAME, typed. A checkbox is too cheap for bytes that do not come back.
	//   4. `force` appears ONLY when the refusal says the warehouse is protected, and says plainly what
	//      it does and does not turn off: the protection flag, never the authorization.
	//
	// Nothing is remembered between opens. Consent given for one warehouse (or one attempt) is not
	// consent for the next.
	import { Dialog } from '@rask/ui/dialog';
	import { Checkbox } from '@rask/ui/checkbox';
	import { toast } from 'svelte-sonner';
	import type { WarehouseRecord } from './catalog';
	import { deleteWarehouse } from './remote/warehouses.remote';

	let {
		open = $bindable(false),
		warehouse,
		ondeleted,
	}: {
		open?: boolean;
		/** The row being deleted. `null` renders nothing — the dialog is opened WITH a target. */
		warehouse: WarehouseRecord | null;
		/** Fired after a confirmed 200, so the list can re-read without waiting for the control tick. */
		ondeleted?: () => void;
	} = $props();

	let busy = $state(false);
	/** The last RFC 9457 `detail`, rendered verbatim. Never rewritten, never summarised. */
	let problem = $state<string | null>(null);
	let problemStatus = $state(0);
	/** Parsed out of a 409 that reported bound namespaces — what a cascade would actually drop. */
	let bound = $state<string[]>([]);
	/** Set by a 409 that reported the `protected` flag — the only thing `force` can turn. */
	let blockedByProtection = $state(false);
	let cascade = $state(false);
	let force = $state(false);
	let purge = $state(false);
	let bucketTyped = $state('');

	/** The purge is armed only while the typed name still MATCHES. Editing the field after checking the
	 *  box disarms it — the consent is to destroy *this* bucket, not to have once typed its name. */
	const purgeArmed = $derived(purge && !!warehouse && bucketTyped === warehouse.bucket);
	const canConfirmPurge = $derived(!!warehouse && bucketTyped === warehouse.bucket);

	function reset(): void {
		problem = null;
		problemStatus = 0;
		bound = [];
		blockedByProtection = false;
		cascade = false;
		force = false;
		purge = false;
		bucketTyped = '';
	}

	// Clear on the closed→open transition AND whenever the dialog is re-pointed at a different
	// warehouse. `armedFor` is deliberately NOT `$state`: it is bookkeeping for this effect, and making
	// it reactive would have the effect depend on its own write.
	//
	// This is a TRANSITION, not a projection, so `$derived` cannot express it: every field reset here is
	// then edited by the user, and a derived value would snap each keystroke back to the reset. The
	// effect reads only `open` and `warehouse.id` and writes none of them, so it cannot re-enter.
	let armedFor: string | null = null;
	$effect(() => {
		const key = open ? (warehouse?.id ?? '') : null;
		if (key === armedFor) return;
		armedFor = key;
		if (key !== null) reset();
	});

	// The catalog's emptiness refusal names what blocks the delete:
	//
	//   warehouse 'acme-wh' still holds 2 namespace(s): bronze, silver. Drop them first, or pass
	//   ?cascade=true to drop exactly those with the warehouse.
	//
	// There is no bindings READ api — the warehouse detail page derives its namespace list from the
	// table registry by naming convention and says so — which makes this refusal the only authoritative
	// statement of what a cascade would take. Namespace ids are DNS-safe by the catalog's own
	// `CONTROL_ID_RE` (lowercase alphanumerics and hyphens), so they cannot contain the '.' that ends
	// the list. A detail that does NOT match is not forced into the shape: it still renders verbatim,
	// and the cascade opt-in is simply not offered, because we could not say what it would drop.
	const BOUND_RE = /still holds \d+ namespace\(s\):\s*([^.]+)\./;
	function boundNamespacesIn(detail: string): string[] {
		const match = BOUND_RE.exec(detail);
		return (match?.[1] ?? '')
			.split(',')
			.map((name) => name.trim())
			.filter(Boolean);
	}

	async function submit(): Promise<void> {
		const target = warehouse;
		if (busy || !target) return;
		busy = true;
		problem = null;
		problemStatus = 0;
		try {
			const res = await deleteWarehouse({
				id: target.id,
				cascade,
				purgeBucket: purgeArmed,
				force,
			});
			if (res.ok) {
				// Built from the RESPONSE, never from the request: the catalog reports each store
				// separately precisely so a partial outcome is diagnosable instead of assumed.
				const r = res.data;
				const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;
				const parts = [
					r.namespaces_dropped.length
						? `dropped ${plural(r.namespaces_dropped.length, 'namespace')} (${r.namespaces_dropped.join(', ')})`
						: 'no namespaces dropped',
					`revoked ${plural(r.tuples_revoked, 'tuple')}`,
					r.bucket_purged
						? `purged bucket ${r.bucket} — ${plural(r.objects_purged, 'object')} destroyed`
						: `bucket ${r.bucket} kept`,
				];
				toast.success(`Deleted warehouse ${r.id}: ${parts.join('; ')}.`);
				open = false;
				ondeleted?.();
				return;
			}
			problem = res.detail;
			problemStatus = res.status;
			if (res.status === 409) {
				const named = boundNamespacesIn(res.detail);
				if (named.length > 0) bound = named;
				// The protection refusal is the same 409 status with a different sentence — it is the
				// sentence, not the code, that says which lock is closed.
				if (/is protected against deletion/.test(res.detail)) blockedByProtection = true;
			}
		} finally {
			busy = false;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content>
		{#if warehouse}
			<Dialog.Title>Delete warehouse {warehouse.id}</Dialog.Title>
			<Dialog.Description>
				Removes the registry record and the access grants on it. The bucket
				<code>{warehouse.bucket}</code> and everything in it survives unless you ask for the purge below.
			</Dialog.Description>

			<div class="body">
				{#if problem}
					<!-- VERBATIM. The catalog writes these to name the fix; paraphrasing loses the fix. -->
					<p class="problem" data-testid="delete-problem">{problem}</p>
					{#if problemStatus === 404}
						<p class="hint">
							The catalog answers 404 identically for a warehouse that does not exist and one you
							may not administer — deliberately, so nobody can probe which ids exist.
						</p>
					{/if}
				{/if}

				{#if bound.length > 0}
					<section class="opt">
						<label class="check">
							<Checkbox bind:checked={cascade} aria-label="Drop the bound namespaces (cascade)" />
							<span
								>Also drop the {bound.length} namespace{bound.length === 1 ? '' : 's'} bound to this warehouse</span
							>
						</label>
						<ul class="named mono" data-testid="cascade-namespaces">
							{#each bound as ns (ns)}
								<li>{ns}</li>
							{/each}
						</ul>
						<p class="hint">
							Exactly these are dropped, one at a time. A namespace that still holds tables refuses
							on its own rung — empty it first.
						</p>
					</section>
				{/if}

				{#if blockedByProtection}
					<section class="opt">
						<label class="check">
							<Checkbox bind:checked={force} aria-label="Override deletion protection (force)" />
							<span>Override deletion protection</span>
						</label>
						<p class="hint">
							Force turns off the <code>protected</code> flag and nothing else. It is not an authorization
							override — the same project-admin rung is still required, checked identically with or without
							it.
						</p>
					</section>
				{/if}

				<section class="opt danger">
					<p class="lead">Permanently destroy the bucket</p>
					<p class="hint">
						Deletes every object in <code>{warehouse.bucket}</code> and the bucket itself. This cannot
						be undone and no other step here touches bytes. Type the bucket name to enable it.
					</p>
					<input
						class="mono"
						bind:value={bucketTyped}
						placeholder={warehouse.bucket}
						aria-label="Type the bucket name to confirm the purge"
						autocomplete="off"
					/>
					<label class="check">
						<Checkbox
							bind:checked={purge}
							disabled={!canConfirmPurge}
							aria-label="Purge the bucket permanently"
						/>
						<span class:dim={!canConfirmPurge}>Purge {warehouse.bucket} permanently</span>
					</label>
				</section>
			</div>

			<div class="actions">
				<button type="button" class="btn ghost" disabled={busy} onclick={() => (open = false)}>
					Cancel
				</button>
				<button type="button" class="btn destroy" disabled={busy} onclick={submit}>
					{busy ? '…' : purgeArmed ? 'Delete and purge bucket' : 'Delete warehouse'}
				</button>
			</div>
		{/if}
	</Dialog.Content>
</Dialog.Root>

<style>
	.body {
		display: flex;
		flex-direction: column;
		gap: 12px;
		font-size: 12px;
		color: var(--mut);
	}
	.problem {
		margin: 0;
		padding: 8px 10px;
		border: 1px solid color-mix(in srgb, var(--fail) 45%, var(--line));
		border-radius: var(--radius-sm);
		color: var(--fail);
	}
	.opt {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 8px 10px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
	}
	.opt.danger {
		border-color: color-mix(in srgb, var(--fail) 40%, var(--line));
	}
	.lead {
		margin: 0;
		color: var(--ink);
		font-weight: 600;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--ink);
	}
	.dim {
		color: var(--faint);
	}
	.named {
		margin: 0;
		padding-left: 18px;
		color: var(--ink);
	}
	.hint {
		margin: 0;
		color: var(--faint);
	}
	input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 9px;
		font-size: 12px;
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.btn.destroy {
		border-color: color-mix(in srgb, var(--fail) 55%, var(--line));
		color: var(--fail);
	}
</style>
