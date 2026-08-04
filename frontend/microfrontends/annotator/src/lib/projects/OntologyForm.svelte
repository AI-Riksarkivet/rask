<script lang="ts">
	// The edit form itself, split out for ONE reason: its fields must start from the stored ontology
	// and then be editable.
	//
	// Seeding them with an `$effect` that assigns state on open is the anti-pattern the Svelte 5
	// autofixer flags, and it is not merely stylistic here — an effect keyed on `open` re-runs
	// whenever its dependencies change, so a project reload behind the dialog could reset fields
	// mid-edit. A component that MOUNTS when the dialog opens gets the same seeding from ordinary
	// `$state(initial)` and cannot re-run at all.
	import { untrack } from 'svelte';
	import { Button } from '@rask/ui/button';
	import { Checkbox } from '@rask/ui/checkbox';
	import { Input } from '@rask/ui/input';
	import { Select } from '@rask/ui/select';

	import { updateProjectOntology } from './remote/projects.remote';
	import type { Project } from './types.js';

	let {
		projectId,
		ontology,
		oncancel,
		onsaved,
	}: {
		projectId: string;
		ontology: Project['ontology'];
		oncancel: () => void;
		onsaved: () => void;
	} = $props();

	const TASK_KINDS = [
		'object-detection',
		'image-segmentation',
		'image-classification',
		'token-classification',
		'image-to-text',
		'document-question-answering',
		'reading-order',
	];
	const SHAPE_KINDS = ['bbox', 'polygon', 'mask', 'segment', 'tag', 'text'];

	// `untrack` because capturing only the INITIAL value is the whole point, and Svelte is right to
	// warn about it by default: a field seeded from a reactive prop usually wants a `$derived`. Here
	// it must not be one — these are editable inputs, and re-deriving them would overwrite whatever
	// the user has typed the moment the project reloads behind the dialog.
	const seed = untrack(() => ontology);
	let classesText = $state(seed.classes.map((c) => c.name).join(', '));
	let shapeKind = $state(seed.classes.find((c) => c.tools.length > 0)?.tools[0] ?? 'bbox');
	let taskKind = $state(seed.kind || 'free');
	let classesRequired = $state(seed.classes.length > 0 && seed.classes.every((c) => c.required));
	let saving = $state(false);
	let error = $state('');

	/** What this form cannot express, so it can refuse rather than flatten it.
	 *
	 *  Deliberately the same three fields the create dialog offers and no more. A per-class tool
	 *  editor and a relation editor are real pieces of work with their own UI questions; a half one
	 *  here would let a manager silently drop structure they never saw. */
	const wouldLose = $derived.by(() => {
		const lost: string[] = [];
		if (ontology.relations.length > 0) lost.push(`${ontology.relations.length} relation(s)`);
		if (ontology.classes.some((c) => c.attributes.length > 0)) lost.push('per-class attributes');
		if (new Set(ontology.classes.map((c) => c.tools.join('/'))).size > 1) {
			lost.push('per-class tools');
		}
		return lost;
	});

	const classNames = $derived(
		classesText
			.split(',')
			.map((c) => c.trim())
			.filter(Boolean),
	);

	async function save(): Promise<void> {
		if (saving || wouldLose.length > 0) return;
		saving = true;
		error = '';
		const result = await updateProjectOntology({
			projectId,
			ontology: {
				...(taskKind === 'free' ? {} : { kind: taskKind }),
				classes: classNames.map((name) => ({
					name,
					tools: [shapeKind],
					required: classesRequired,
				})),
			},
		});
		saving = false;
		if (!result.ok) {
			// The SERVER's own words — a 409 past `labeling`, a 403, or the model's cross-check naming
			// an undeclared class. Each is actionable, and none of them is "save failed".
			error = result.detail;
			return;
		}
		onsaved();
	}
</script>

{#if wouldLose.length > 0}
	<p class="text-destructive text-sm" data-testid="ontology-too-rich">
		This task declares {wouldLose.join(' and ')}, which this form cannot edit. Editing here would drop
		them, so it is disabled — change the ontology through the API until the full editor lands.
	</p>
{:else}
	<div class="flex flex-col gap-3">
		<label class="flex flex-col gap-1 text-sm">
			<span>Label classes <span class="text-muted-foreground">(comma-separated)</span></span>
			<Input bind:value={classesText} placeholder="person, ship, signature" />
		</label>
		<label class="flex flex-col gap-1 text-sm">
			<span>Shape kind</span>
			<Select
				bind:value={shapeKind}
				ariaLabel="Shape kind"
				options={SHAPE_KINDS.map((kind) => ({ value: kind, label: kind }))}
			/>
		</label>
		<label class="flex flex-col gap-1 text-sm">
			<span>Task type</span>
			<Select
				bind:value={taskKind}
				ariaLabel="Task type"
				options={[
	{ value: 'free', label: 'free (no task type)' },
	...TASK_KINDS.map((kind) => ({ value: kind, label: kind })),
]}
			/>
		</label>
		<label class="flex items-center gap-2 text-sm">
			<Checkbox bind:checked={classesRequired} aria-label="Every class is required" />
			<span>Every class is required</span>
		</label>
		<!-- Said out loud, because it is the property that makes editing safe at all: items already
		     sent captured their own copy, so work in review is judged by the contract it was issued
		     under. Without that, an edit mid-flight would retroactively invalidate someone's work. -->
		<p class="text-muted-foreground text-xs">
			Items already sent keep the definition they were issued under. This applies to items sent from
			now on.
		</p>
		{#if error}
			<p class="text-destructive text-sm" data-testid="ontology-error">{error}</p>
		{/if}
		<div class="flex justify-end gap-2">
			<Button variant="ghost" onclick={oncancel}>Cancel</Button>
			<Button disabled={saving} onclick={save}>{saving ? 'Saving…' : 'Save'}</Button>
		</div>
	</div>
{/if}
