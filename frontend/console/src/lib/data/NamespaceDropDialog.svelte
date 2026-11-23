<script lang="ts">
	// #85 drop a namespace: the namespaces page's confirm. Restrict by default (a non-empty namespace is
	// refused; an empty one goes for good, with nothing in it to recover), Cascade opt-in. The page mounts
	// one per opening, so the cascade choice and the recovery read belong to that opening alone.
	import { untrack } from 'svelte';
	import { AlertDialog } from '@riksarkivet/ui/alert-dialog';
	import { buttonVariants } from '@riksarkivet/ui/button';
	import { Checkbox } from '@riksarkivet/ui/checkbox';
	import { dropRecoveryText } from './drop-recovery';
	import { DropRecoveryRead } from './drop-recovery-read.svelte.ts';
	import type { Banner } from './NamespaceCreateForm.svelte';
	import { dropNamespace } from './remote/namespace.api';

	let {
		open = $bindable(),
		ns,
		count,
		busy = $bindable(),
		onresult,
		onchanged,
		ondone,
	}: {
		open: boolean;
		ns: string;
		/** The tables inside, which sizes the Cascade choice honestly. */
		count: number;
		/** One write at a time on the page: the drop and the create share it. */
		busy: boolean;
		onresult: (banner: Banner | null) => void;
		onchanged: () => Promise<void>;
		/** The confirm settled, whatever its answer: the dialog is closed and disarmed. */
		ondone: () => void;
	} = $props();

	let cascade = $state(false);
	// What a cascade would leave recoverable, read from the namespace's warehouse as the dialog opens:
	// once, for the namespace this opening is for.
	const recovery = new DropRecoveryRead();
	untrack(() => void recovery.load(ns));

	function fail(status: number, detail: string): void {
		if (status === 401)
			onresult({ tone: 'fail', text: 'Sign in — dropping a namespace is a per-user action.' });
		else if (status === 403)
			onresult({
				tone: 'fail',
				text: `Denied: dropping ${ns} needs the modifier rung (can_delete).`,
			});
		else if (status === 0)
			onresult({ tone: 'fail', text: 'Catalog unreachable — the drop was not applied.' });
		else onresult({ tone: 'fail', text: detail });
	}

	async function confirm(): Promise<void> {
		if (busy) return;
		busy = true;
		onresult(null);
		try {
			const res = await dropNamespace({ namespace: ns, cascade });
			if (res.ok) {
				onresult({ tone: 'ok', text: `namespace ${ns} dropped${cascade ? ' (cascade)' : ''}` });
				await onchanged();
			} else fail(res.status, res.detail);
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			onresult({ tone: 'fail', text: `drop response drifted from the contract: ${String(err)}` });
		} finally {
			// ALWAYS close + disarm: bits-ui's AlertDialog.Action does not auto-close, so leaving the
			// dialog open would keep the destructive action armed for a second, confirm-free fire. The
			// banner carries success or failure either way.
			busy = false;
			open = false;
			ondone();
		}
	}
</script>

<AlertDialog.Root bind:open>
	<AlertDialog.Content>
		<AlertDialog.Title>Drop namespace {ns}</AlertDialog.Title>
		<AlertDialog.Description>
			This drops <span class="mono">{ns}</span> from the catalog (owner-gated: can_delete). The
			default Restrict behavior refuses a non-empty namespace, and removes an empty one permanently:
			there is nothing in it to recover. Tick Cascade to also drop everything inside it.
			{#if cascade}
				A cascade drops it and its tables
				{#if recovery.current}{dropRecoveryText(recovery.current)}.{:else}(reading how long they
					stay recoverable…).{/if}
			{/if}
		</AlertDialog.Description>
		<label class="text-muted-foreground flex items-center gap-2 text-[13px]">
			<Checkbox bind:checked={cascade} disabled={busy} />
			Cascade — also drop the {count} table{count === 1 ? '' : 's'} inside
		</label>
		<div class="flex justify-end gap-2">
			<AlertDialog.Cancel disabled={busy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class={buttonVariants({ variant: 'destructive' })}
				disabled={busy}
				onclick={confirm}
			>
				Drop
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>
