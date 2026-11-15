<script lang="ts">
	/**
	 * The Labels section of a project's page (ADR 0211): every label the project defines (its kind,
	 * the kinds of object it applies to, its allowed values and description), a form that defines one
	 * or evolves the definition its name holds, and a delete per definition. Defining and deleting are
	 * the project admin's: like the page's other admin actions, the controls are offered to whoever
	 * reads the project and the catalog's refusal is said in plain words. An evolution may only grow a
	 * label's scope and allowed values and never change its kind, so every label already attached stays
	 * valid; the catalog refuses anything else (400) and a delete while any object carries it (409).
	 */
	import { Trash2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { Checkbox } from '@rask/ui/checkbox';
	import { Input } from '@rask/ui/input';
	import { Select } from '@rask/ui/select';
	import * as Table from '@rask/ui/table';
	import {
		KIND_NAMES,
		LABEL_KINDS,
		type LabelDefinition,
		type LabelKind,
		type LabelScope,
	} from './labels.ts';
	import { defineLabel, deleteLabel, fetchLabelDefinitions } from './remote/labels.api.ts';

	let { project }: { project: string } = $props();

	const read = $derived(fetchLabelDefinitions(project));
	const definitions = $derived(read.current);
	const sorted = $derived(
		definitions?.ok ? [...definitions.data].sort((a, b) => a.name.localeCompare(b.name)) : [],
	);

	let name = $state('');
	let description = $state('');
	let kind = $state<LabelKind>('marker');
	let onNamespaces = $state(false);
	let onTables = $state(true);
	let allowed = $state('');
	/** The definition the form evolves, when it was opened from one: its kind is fixed. */
	let evolving = $state<LabelDefinition | null>(null);
	let busy = $state(false);
	let message = $state<{ ok: boolean; text: string } | null>(null);

	const scope = $derived<LabelScope[]>([
		...(onNamespaces ? (['namespace'] as const) : []),
		...(onTables ? (['table'] as const) : []),
	]);
	const allowedValues = $derived(
		allowed
			.split(',')
			.map((v) => v.trim())
			.filter((v) => v !== ''),
	);
	const hint = $derived(
		!name.trim()
			? 'Name the label.'
			: scope.length === 0
				? 'Choose what it applies to: namespaces, tables or both.'
				: kind === 'enumerated' && allowedValues.length === 0
					? 'List its allowed values, separated by commas.'
					: null,
	);

	function chooseKind(value: string): void {
		const chosen = LABEL_KINDS.find((k) => k === value);
		if (chosen) kind = chosen;
	}

	function evolve(d: LabelDefinition): void {
		evolving = d;
		name = d.name;
		description = d.description ?? '';
		kind = d.kind;
		onNamespaces = d.scope.includes('namespace');
		onTables = d.scope.includes('table');
		allowed = (d.allowed_values ?? []).join(', ');
		message = null;
	}

	function reset(): void {
		evolving = null;
		name = '';
		description = '';
		kind = 'marker';
		onNamespaces = false;
		onTables = true;
		allowed = '';
	}

	function fail(status: number, detail: string, act: string): void {
		if (status === 401) message = { ok: false, text: `Sign in to ${act} a label.` };
		else if (status === 403)
			message = {
				ok: false,
				text: `Denied: ${act === 'delete' ? 'deleting' : 'defining'} a label needs project-admin access (can_administer).`,
			};
		else if (status === 0)
			message = {
				ok: false,
				text: `Not confirmed: the catalog did not answer, so the label may have been ${act === 'delete' ? 'deleted' : 'defined'}.`,
			};
		else message = { ok: false, text: detail };
	}

	async function submit(): Promise<void> {
		if (busy || hint !== null) return;
		busy = true;
		message = null;
		const label = name.trim();
		try {
			const res = await defineLabel({
				project,
				label: {
					name: label,
					description: description.trim() || undefined,
					scope,
					kind,
					allowed_values: kind === 'enumerated' ? allowedValues : undefined,
				},
			});
			if (res.ok) {
				message = { ok: true, text: `${evolving ? 'Updated' : 'Defined'} label ${label}.` };
				reset();
			} else fail(res.status, res.detail, 'define');
		} finally {
			busy = false;
		}
	}

	async function remove(d: LabelDefinition): Promise<void> {
		if (busy) return;
		busy = true;
		message = null;
		try {
			const res = await deleteLabel({ project, name: d.name });
			if (res.ok) {
				message = { ok: true, text: `Deleted label ${d.name}.` };
				if (evolving?.name === d.name) reset();
			} else fail(res.status, res.detail, 'delete');
		} finally {
			busy = false;
		}
	}
</script>

<section class="mb-6" aria-labelledby="project-labels-heading" data-testid="project-labels">
	<h2 id="project-labels-heading" class="mb-2 text-sm font-semibold">Labels</h2>
	<p class="text-muted-foreground mb-2 max-w-[62ch] text-xs">
		Labels this project defines, which an owner attaches to a table or a namespace in it to describe
		it and find it by. A label grants and refuses nothing.
	</p>

	{#if definitions === undefined}
		<p class="text-muted-foreground text-xs">Loading labels…</p>
	{:else if !definitions.ok}
		<p class="text-muted-foreground text-xs">
			The labels could not be read (HTTP {definitions.status}): {definitions.detail}
		</p>
	{:else if sorted.length === 0}
		<p class="text-muted-foreground text-xs">This project defines no label yet.</p>
	{:else}
		<Table.Root class="text-xs">
			<Table.Header>
				<Table.Row>
					<Table.Head>label</Table.Head>
					<Table.Head>kind</Table.Head>
					<Table.Head>applies to</Table.Head>
					<Table.Head>allowed values</Table.Head>
					<Table.Head>description</Table.Head>
					<Table.Head><span class="sr-only">actions</span></Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each sorted as d (d.name)}
					<Table.Row>
						<Table.Cell class="font-mono">{d.name}</Table.Cell>
						<Table.Cell>{KIND_NAMES[d.kind]}</Table.Cell>
						<Table.Cell>{d.scope.map((s) => `${s}s`).join(', ')}</Table.Cell>
						<Table.Cell class="font-mono">{(d.allowed_values ?? []).join(', ') || '—'}</Table.Cell>
						<Table.Cell class="whitespace-normal">{d.description ?? ''}</Table.Cell>
						<Table.Cell class="text-right">
							<Button
								variant="ghost"
								size="sm"
								aria-label="Evolve label {d.name}"
								onclick={() => evolve(d)}>Evolve</Button
							>
							<Button
								variant="ghost"
								size="sm"
								aria-label="Delete label {d.name}"
								disabled={busy}
								onclick={() => void remove(d)}
							>
								<Trash2 size={12} />
							</Button>
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	{/if}

	{#if definitions?.ok}
		<form
			class="border-border mt-3 flex flex-wrap items-end gap-3 rounded-md border p-3 text-xs"
			aria-label={evolving ? `Evolve label ${evolving.name}` : 'Define a label'}
			onsubmit={(e) => {
				e.preventDefault();
				void submit();
			}}
		>
			<label class="text-muted-foreground flex flex-col gap-1">
				Name
				<Input
					class="h-8 w-48 font-mono"
					bind:value={name}
					placeholder="pii or team.owner"
					aria-label="Label name"
					disabled={evolving !== null}
				/>
			</label>
			<label class="text-muted-foreground flex flex-col gap-1">
				Kind
				<Select
					value={kind}
					onValueChange={chooseKind}
					ariaLabel="Label kind"
					disabled={evolving !== null}
					options={LABEL_KINDS.map((k) => ({ value: k, label: KIND_NAMES[k] }))}
				/>
			</label>
			<fieldset class="text-muted-foreground flex flex-col gap-1">
				<legend class="mb-1">Applies to</legend>
				<span class="flex gap-3">
					<label class="flex items-center gap-1.5"
						><Checkbox
							bind:checked={onNamespaces}
							disabled={evolving?.scope.includes('namespace') ?? false}
						/> namespaces</label
					>
					<label class="flex items-center gap-1.5"
						><Checkbox
							bind:checked={onTables}
							disabled={evolving?.scope.includes('table') ?? false}
						/> tables</label
					>
				</span>
			</fieldset>
			{#if kind === 'enumerated'}
				<label class="text-muted-foreground flex flex-col gap-1">
					Allowed values
					<Input
						class="h-8 w-56 font-mono"
						bind:value={allowed}
						placeholder="public, internal, restricted"
						aria-label="Allowed values"
					/>
				</label>
			{/if}
			<label class="text-muted-foreground flex min-w-48 flex-1 flex-col gap-1">
				Description
				<Input class="h-8" bind:value={description} aria-label="Label description" />
			</label>
			<div class="flex items-center gap-2">
				<Button
					type="submit"
					size="sm"
					disabled={busy || hint !== null}
					aria-describedby={hint ? 'label-define-hint' : undefined}
				>
					{evolving ? 'Save label' : 'Define label'}
				</Button>
				{#if evolving}
					<Button variant="ghost" size="sm" onclick={reset}>Cancel</Button>
				{/if}
			</div>
			{#if hint}
				<p class="text-muted-foreground basis-full" id="label-define-hint">{hint}</p>
			{:else if evolving}
				<p class="text-muted-foreground basis-full">
					A label’s kind never changes, and what it applies to and its allowed values only grow, so
					every label already attached stays valid.
				</p>
			{/if}
		</form>
	{/if}
	{#if message}
		<p class={['mt-2 text-xs', message.ok ? 'text-success' : 'text-destructive']} role="status">
			{message.text}
		</p>
	{/if}
</section>
