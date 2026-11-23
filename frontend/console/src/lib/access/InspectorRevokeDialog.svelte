<script lang="ts">
	// The inspector's revoke confirm, with the blast radius measured BEFORE the write. "Are you sure?" is
	// not an answer to "how many": a tuple high in the hierarchy can cut access for every principal below
	// it, and the only honest confirm names that number first.
	import { AlertDialog } from '@riksarkivet/ui/alert-dialog';
	import { TriangleAlert } from '@lucide/svelte';
	import type { Tuple } from '../access';
	import { deleteTuple, listUsers } from '../remote/access.api';

	type Props = {
		/** The tuple to revoke; `null` keeps the dialog closed. */
		target: Tuple | null;
		onclose: () => void;
		/** Called with the write's outcome while the dialog is still open, so the caller's refresh lands
		 *  before it closes. */
		ondone: (message: { ok: boolean; text: string }) => Promise<void>;
	};

	let { target, onclose, ondone }: Props = $props();

	let busy = $state(false);

	// Who holds the relation on the object now, read when the dialog opens on a tuple.
	const holders = $derived(
		target ? listUsers({ object: target.object, relation: target.relation }) : null,
	);
	const measuring = $derived(holders?.loading ?? false);
	// A failed read says "could not measure" — never a reassuring zero.
	const blast = $derived.by(() => {
		const res = holders?.current;
		return res?.ok ? { count: res.data.users.length, truncated: res.data.truncated } : null;
	});

	async function revoke(): Promise<void> {
		const t = target;
		if (!t || busy) return;
		busy = true;
		try {
			const res = await deleteTuple(t);
			await ondone(
				res.ok
					? { ok: true, text: `Revoked (${t.user}, ${t.relation}, ${t.object}).` }
					: { ok: false, text: res.status === 403 ? 'Denied: estate-admin only.' : res.detail },
			);
		} finally {
			busy = false;
			onclose();
		}
	}
</script>

<AlertDialog.Root
	open={target !== null}
	onOpenChange={(open) => {
		if (!open) onclose();
	}}
>
	<AlertDialog.Content>
		<AlertDialog.Title>Revoke this tuple</AlertDialog.Title>
		<AlertDialog.Description>
			{#if target}
				<span class="font-mono">({target.user}, {target.relation}, {target.object})</span>
			{/if}
		</AlertDialog.Description>

		<div class="flex items-start gap-2 rounded border border-warning/50 bg-warning/10 p-2 text-xs">
			<TriangleAlert size={14} class="mt-0.5 shrink-0 text-warning" />
			{#if measuring}
				<span>Measuring blast radius…</span>
			{:else if blast}
				<span>
					<strong class="tabular-nums">{blast.count}</strong>
					principal{blast.count === 1 ? '' : 's'} currently hold
					<span class="font-mono">{target?.relation}</span> here.
					{#if blast.truncated}
						At least — the store truncated the list, so the real number is higher.
					{/if}
				</span>
			{:else}
				<span>
					Could not measure the blast radius. Revoking anyway may remove access for principals this
					dialog cannot name.
				</span>
			{/if}
		</div>

		<div class="mt-2 flex justify-end gap-2">
			<AlertDialog.Cancel disabled={busy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="border-destructive/40 bg-destructive/15 text-destructive hover:bg-destructive/25"
				disabled={busy}
				onclick={revoke}
			>
				Revoke
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>
