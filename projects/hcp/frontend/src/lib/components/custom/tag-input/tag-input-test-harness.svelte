<script lang="ts">
	import TagInput from './tag-input.svelte';

	let tags = $state(['production', 'critical', 'eu-west']);
</script>

<div class="max-w-md space-y-4 p-4">
	<h3 class="text-sm font-medium">Tag Input Test Harness</h3>
	<TagInput bind:tags placeholder="Add tag..." />
	<div data-testid="tag-count" class="text-xs text-muted-foreground">
		{tags.length} tag(s)
	</div>
</div>
