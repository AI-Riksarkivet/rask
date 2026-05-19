<script lang="ts">
	import SaveButton from './save-button.svelte';

	let dirty = $state(false);
	let saving = $state(false);
	let saveCount = $state(0);

	function handleSave() {
		saving = true;
		saveCount++;
		setTimeout(() => {
			saving = false;
			dirty = false;
		}, 500);
	}
</script>

<div class="space-y-3 p-4">
	<div class="flex gap-2">
		<button
			class="rounded border px-3 py-1 text-sm"
			onclick={() => (dirty = true)}
			data-testid="make-dirty"
		>
			Make dirty
		</button>
	</div>

	<SaveButton {dirty} {saving} onclick={handleSave} />

	<p class="text-xs text-muted-foreground" data-testid="save-count">
		Saves: {saveCount}
	</p>
</div>
