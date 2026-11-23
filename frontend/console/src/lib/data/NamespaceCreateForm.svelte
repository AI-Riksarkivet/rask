<script lang="ts" module>
	/** What the namespaces page says about the last write it sent: its banner. */
	export type Banner = { tone: 'ok' | 'fail'; text: string };
</script>

<script lang="ts">
	// The namespaces page's create: a namespace directly under a warehouse, named `<warehouse>$<name>`,
	// through the spec's CreateNamespace (`createWarehouseNamespace`). Mounted with the page and shown
	// behind its header's toggle, so the draft survives closing the form; the outcome goes up to the
	// page's banner, which the drop shares.
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { DELIMITER } from '#lib/api/identifiers.ts';
	import { missingFieldsHint } from '#lib/form-hint.ts';
	import { createWarehouseNamespace } from './remote/warehouses.api';

	let {
		open,
		warehouses,
		busy = $bindable(),
		onresult,
		onchanged,
	}: {
		open: boolean;
		/** The warehouses the root listing answered, to create in. */
		warehouses: readonly string[];
		/** One write at a time on the page: the drop and the create share it. */
		busy: boolean;
		onresult: (banner: Banner | null) => void;
		/** Read the lists again: the write changed them. */
		onchanged: () => Promise<void>;
	} = $props();

	let draft = $state({ warehouse: '', name: '' });
	const hint = $derived(
		draft.name.includes(DELIMITER)
			? `A name holds no ${DELIMITER}: this creates a namespace directly under the warehouse.`
			: missingFieldsHint('Creating', [
					['a warehouse', draft.warehouse],
					['a name', draft.name.trim()],
				]),
	);

	async function create(): Promise<void> {
		const warehouse = draft.warehouse;
		const name = draft.name.trim();
		if (busy || hint !== null) return;
		busy = true;
		onresult(null);
		try {
			const res = await createWarehouseNamespace({ warehouse, name });
			if (res.ok) {
				onresult({ tone: 'ok', text: `namespace ${warehouse}${DELIMITER}${name} created` });
				draft = { warehouse, name: '' };
				await onchanged();
			} else if (res.status === 401)
				onresult({ tone: 'fail', text: 'Sign in — creating a namespace is a per-user action.' });
			else if (res.status === 403)
				onresult({
					tone: 'fail',
					text: `Denied: creating a namespace in ${warehouse} needs write access there (can_create_namespace). ${res.detail}`,
				});
			else if (res.status === 0)
				onresult({
					tone: 'fail',
					text: 'Not confirmed: the catalog did not answer, so the namespace may have been created.',
				});
			else onresult({ tone: 'fail', text: res.detail });
		} finally {
			busy = false;
		}
	}
</script>

{#if open}
	<form
		id="create-namespace"
		class="mb-3 flex flex-wrap items-end gap-2"
		aria-label="Create namespace"
		onsubmit={(e) => {
			e.preventDefault();
			void create();
		}}
	>
		<label class="text-muted-foreground flex flex-col gap-1 text-xs">
			Warehouse
			<Select
				bind:value={draft.warehouse}
				ariaLabel="Warehouse"
				placeholder="warehouse…"
				options={warehouses.map((w) => ({ value: w, label: w }))}
			/>
		</label>
		<label class="text-muted-foreground flex flex-col gap-1 text-xs">
			Name
			<Input class="w-48 font-mono" bind:value={draft.name} placeholder="bronze" />
		</label>
		<Button
			type="submit"
			disabled={busy || hint !== null}
			aria-describedby={hint ? 'create-hint' : undefined}
		>
			Create
		</Button>
		{#if hint}<p class="text-muted-foreground m-0 basis-full text-xs" id="create-hint">
				{hint}
			</p>{/if}
	</form>
{/if}
