<script lang="ts">
	// The warehouses page's Provision form: a warehouse under a project, its bucket defaulting to its
	// id. Project-admin gated by the catalog (can_create_warehouse); the outcome goes up to the page's
	// banner, and one write at a time runs on the page, so it shares the lifecycle actions' busy flag.
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { missingFieldsHint } from '#lib/form-hint.ts';
	import type { Warehouse } from './catalog';
	import { createWarehouse } from './remote/warehouses.api';

	let {
		busy = $bindable(),
		onstart,
		onprovisioned,
		onfailed,
	}: {
		busy: boolean;
		/** A write is going out: the last outcome no longer describes the page. */
		onstart: () => void;
		/** The catalog provisioned it; the page says so and reads its list again. */
		onprovisioned: (warehouse: Warehouse) => Promise<void>;
		onfailed: (status: number, detail: string) => void;
	} = $props();

	let draft = $state({ id: '', project: '', bucket: '' });
	const hint = $derived(
		missingFieldsHint('Provisioning', [
			['a warehouse id', draft.id],
			['a project', draft.project],
		]),
	);

	async function provision(): Promise<void> {
		if (busy || !draft.id.trim() || !draft.project.trim()) return;
		busy = true;
		onstart();
		try {
			const res = await createWarehouse({
				id: draft.id.trim(),
				project: draft.project.trim(),
				bucket: draft.bucket.trim() || null,
			});
			if (res.ok) {
				draft = { id: '', project: '', bucket: '' };
				await onprovisioned(res.data);
			} else onfailed(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	const LABEL = 'text-muted-foreground flex flex-col gap-1 text-xs';
	const FIELD = 'w-44 font-mono';
</script>

<section>
	<h2 class="text-muted-foreground mt-[18px] mb-2 text-[13px] tracking-[0.06em] uppercase">
		Provision
	</h2>
	<form
		class="flex flex-wrap items-end gap-2"
		onsubmit={(e) => {
			e.preventDefault();
			void provision();
		}}
	>
		<!-- Each field named on screen, not by a placeholder that goes when the reader types. -->
		<label class={LABEL}>
			Warehouse id
			<Input class={FIELD} bind:value={draft.id} placeholder="acme-wh" />
		</label>
		<label class={LABEL}>
			Project
			<Input class={FIELD} bind:value={draft.project} placeholder="acme" />
		</label>
		<label class={LABEL}>
			Bucket
			<Input class={FIELD} bind:value={draft.bucket} placeholder="defaults to the id" />
		</label>
		<Button
			type="submit"
			disabled={busy || hint !== null}
			aria-describedby={hint ? 'provision-hint' : undefined}
		>
			Provision
		</Button>
	</form>
	{#if hint}<p class="text-muted-foreground text-xs" id="provision-hint">{hint}</p>{/if}
</section>
