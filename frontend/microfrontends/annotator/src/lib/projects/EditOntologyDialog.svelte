<script lang="ts">
	// Edit a project's task definition after create.
	//
	// There was no way to: an ontology was set once and never again, so a taxonomy typo meant a new
	// project. That is a poor enough answer on its own, and it gets worse the moment classes are the
	// enforceable contract — a closed set you cannot correct is a closed set people work around.
	//
	// A shell, deliberately. `{#if open}` means the form MOUNTS on open, so its fields seed from the
	// stored ontology through ordinary `$state(initial)` rather than an effect that assigns state.
	import { Dialog } from '@rask/ui/dialog';

	import OntologyForm from './OntologyForm.svelte';
	import type { Project } from './types.js';

	let {
		projectId,
		ontology,
		open = $bindable(false),
		onsaved,
	}: {
		projectId: string;
		ontology: Project['ontology'];
		open?: boolean;
		onsaved: () => void;
	} = $props();
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="sm:max-w-md" data-testid="edit-ontology">
		<Dialog.Title>Edit the labeling task</Dialog.Title>
		<Dialog.Description>
			The classes annotators may use, and the geometry they may draw. Enforced at submit.
		</Dialog.Description>
		<!-- `{#if open}` so the form MOUNTS on open: its fields then seed from the stored ontology
		     through ordinary `$state(initial)` rather than an effect that assigns state. -->
		{#if open}
			<OntologyForm
				{projectId}
				{ontology}
				oncancel={() => (open = false)}
				onsaved={() => {
					open = false;
					onsaved();
				}}
			/>
		{/if}
	</Dialog.Content>
</Dialog.Root>
