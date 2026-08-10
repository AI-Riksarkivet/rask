<script lang="ts">
	import { Switch } from 'bits-ui';
	import { cn } from '../../utils/index.js';

	let {
		checked = $bindable(false),
		class: className,
		...rest
	}: {
		checked?: boolean;
		class?: string;
		'aria-label'?: string;
		/** Fires on user interaction only, like the Slider's `onValueChange`. Lets a caller whose truth
		 *  lives in a PROP write straight to that prop instead of mirroring it into local `$state` and
		 *  syncing back with an `$effect` — a mirror goes stale the moment the parent replaces the prop,
		 *  and then overwrites it. Reaches `Switch.Root` through `...rest`; declared here so it type-checks. */
		onCheckedChange?: (checked: boolean) => void;
		/** Reaches `Switch.Root` through `...rest` and always has — declared here because a prop that
		 *  works at runtime and fails svelte-check is a prop nobody can use. A caller with an in-flight
		 *  write needs it: a switch that can be toggled again mid-request sends the opposite value at
		 *  the server's own pace. */
		disabled?: boolean;
	} = $props();
</script>

<Switch.Root
	bind:checked
	data-slot="switch"
	class={cn(
	'inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors',
	'focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none',
	'data-[state=checked]:bg-primary data-[state=unchecked]:bg-secondary',
	className,
)}
	{...rest}
>
	<Switch.Thumb
		class="bg-background pointer-events-none block size-4 rounded-full shadow transition-transform data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0.5"
	/>
</Switch.Root>
