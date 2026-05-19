<script lang="ts">
	import IpListEditor from './ip-list-editor.svelte';

	let addresses = $state(['10.0.0.0/8', '192.168.1.0/24']);
</script>

<div class="max-w-md space-y-4 p-4">
	<h3 class="text-sm font-medium">IP List Editor Test Harness</h3>
	<IpListEditor bind:addresses label="Allowed IPs" />
	<div data-testid="ip-count" class="text-xs text-muted-foreground">
		{addresses.length} address(es)
	</div>
</div>
