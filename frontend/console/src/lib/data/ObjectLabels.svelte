<script lang="ts">
	/**
	 * The Labels block of a table's or a namespace's Overview (ADR 0211): the labels the object
	 * carries, as chips, and the controls to attach one its project defines for this kind of object or
	 * to remove one. What may be attached is the object's own describe's `available`, read on the
	 * object, so an owner who is no member of the project is offered it too. The catalog decides who
	 * may label (`can_label`, the object owner's), so the controls are offered and a refusal is said in
	 * plain words. An object in no project's warehouse can carry no label: the catalog answers its
	 * describe 400, and the block draws nothing.
	 */
	import { X } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { warehouseOf } from '#lib/api/identifiers.ts';
	import { definitionsFor, labelChip, type LabelDefinition, type LabelScope } from './labels.ts';
	import { fetchObjectLabels, removeObjectLabel, setObjectLabel } from './remote/labels.api.ts';
	import { fetchWarehouses } from './remote/warehouses.api.ts';

	let { kind, id }: { kind: LabelScope; id: string } = $props();

	/** The object's project, when the warehouse registry shows it to the caller: a write refreshes
	 *  that project's labelled objects, which the lists read. */
	const warehousesRead = fetchWarehouses();
	const project = $derived.by((): string | undefined => {
		const res = warehousesRead.current;
		if (!res?.ok) return undefined;
		return res.data.find((w) => w.id === warehouseOf(id))?.project;
	});

	const labelsRead = $derived(fetchObjectLabels({ kind, id }));
	const carried = $derived(labelsRead.current);
	/** The catalog answers 400 for an object in no project's warehouse (the estate's own, whose name
	 *  is its setting): such an object can carry no label, and the block draws nothing for it. The
	 *  block waits for that answer, so it never flashes for an object it will not describe. */
	const shown = $derived(carried !== undefined && (carried.ok || carried.status !== 400));

	const offered = $derived<LabelDefinition[]>(
		carried?.ok ? definitionsFor(carried.data.available ?? [], kind) : [],
	);

	let chosen = $state('');
	let value = $state('');
	let busy = $state(false);
	let message = $state<{ ok: boolean; text: string } | null>(null);

	const definition = $derived(offered.find((d) => d.name === chosen) ?? null);
	const valueMissing = $derived(
		definition !== null && definition.kind !== 'marker' && !value.trim(),
	);

	function choose(name: string): void {
		chosen = name;
		value = '';
		message = null;
	}

	function fail(status: number, detail: string, verb: string): void {
		if (status === 401) message = { ok: false, text: `Sign in to ${verb} a label.` };
		else if (status === 403)
			message = {
				ok: false,
				text: `Denied: ${verb === 'remove' ? 'removing' : 'attaching'} a label on this ${kind} needs its owner’s access (can_label).`,
			};
		else if (status === 0)
			message = {
				ok: false,
				text: `Not confirmed: the catalog did not answer, so the label may have been ${verb === 'remove' ? 'removed' : 'attached'}.`,
			};
		else message = { ok: false, text: detail };
	}

	async function attach(): Promise<void> {
		if (busy || definition === null || valueMissing) return;
		busy = true;
		message = null;
		try {
			const res = await setObjectLabel({
				kind,
				id,
				project,
				name: definition.name,
				value: definition.kind === 'marker' ? undefined : value.trim(),
			});
			if (res.ok) {
				message = { ok: true, text: `Labelled ${definition.name}.` };
				chosen = '';
				value = '';
			} else fail(res.status, res.detail, 'attach');
		} finally {
			busy = false;
		}
	}

	async function remove(name: string): Promise<void> {
		if (busy) return;
		busy = true;
		message = null;
		try {
			const res = await removeObjectLabel({ kind, id, project, name });
			if (res.ok) message = { ok: true, text: `Removed ${name}.` };
			else fail(res.status, res.detail, 'remove');
		} finally {
			busy = false;
		}
	}
</script>

{#if shown}
	<section class="mb-5" aria-labelledby="labels-heading" data-testid="object-labels">
		<h2
			id="labels-heading"
			class="text-muted-foreground mb-2 text-[13px] font-semibold tracking-wider uppercase"
		>
			Labels
		</h2>
		{#if carried && !carried.ok}
			<p class="text-muted-foreground text-xs">
				The labels could not be read (HTTP {carried.status}): {carried.detail}
			</p>
		{:else if carried?.ok}
			{@const names = Object.keys(carried.data.labels).sort()}
			{#if names.length === 0}
				<p class="text-muted-foreground text-xs">This {kind} carries no label.</p>
			{:else}
				<ul class="flex flex-wrap gap-1.5" aria-label="Labels on this {kind}">
					{#each names as name (name)}
						<li>
							<Badge variant="outline" class="gap-1 pr-1 font-mono font-normal">
								{labelChip(name, carried.data.labels[name] ?? null)}
								<button
									type="button"
									class="text-muted-foreground hover:text-foreground rounded-sm disabled:opacity-50"
									aria-label="Remove label {name}"
									disabled={busy}
									onclick={() => void remove(name)}
								>
									<X size={12} />
								</button>
							</Badge>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}

		{#if carried?.ok && offered.length === 0}
			<p class="text-muted-foreground mt-2 text-xs">
				Its project defines no label for a {kind}.
			</p>
		{:else if offered.length > 0}
			<form
				class="mt-2 flex flex-wrap items-center gap-2"
				aria-label="Add a label"
				onsubmit={(e) => {
					e.preventDefault();
					void attach();
				}}
			>
				<Select
					value={chosen}
					ariaLabel="Label to add"
					placeholder="label…"
					options={offered.map((d) => ({ value: d.name, label: d.name }))}
					onValueChange={choose}
				/>
				{#if definition?.kind === 'enumerated'}
					<Select
						bind:value
						ariaLabel="Label value"
						placeholder="value…"
						options={(definition.allowed_values ?? []).map((v) => ({ value: v, label: v }))}
					/>
				{:else if definition?.kind === 'free_text'}
					<Input
						class="h-8 w-56 font-mono"
						bind:value
						maxlength={256}
						placeholder="value"
						aria-label="Label value"
					/>
				{/if}
				<Button type="submit" size="sm" disabled={busy || definition === null || valueMissing}>
					Add label
				</Button>
				{#if definition?.description}
					<p class="text-muted-foreground basis-full text-xs">{definition.description}</p>
				{/if}
			</form>
		{/if}
		{#if message}
			<p class={['mt-2 text-xs', message.ok ? 'text-success' : 'text-destructive']} role="status">
				{message.text}
			</p>
		{/if}
	</section>
{/if}
