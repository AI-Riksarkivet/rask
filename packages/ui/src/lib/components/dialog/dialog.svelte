<script lang="ts">
	import { Dialog as BitsDialog } from 'bits-ui';
	let { open = $bindable(false), children, ...rest }: BitsDialog.RootProps = $props();
</script>

<BitsDialog.Root bind:open {...rest}>
	{@render children?.()}
</BitsDialog.Root>
