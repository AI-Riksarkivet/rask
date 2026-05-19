<script lang="ts">
	import CopyableInput from './copyable-input.svelte';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
</script>

<Tooltip.Provider>
	<div class="max-w-md space-y-6 p-4">
		<CopyableInput value="urn:hcp:namespace:production-data" label="Canonical ID" />
		<CopyableInput value="sk_live_abc123secret456def789" label="Secret Key" secret={true} />
		<CopyableInput value="https://hcp.example.com:8000" />
	</div>
</Tooltip.Provider>
