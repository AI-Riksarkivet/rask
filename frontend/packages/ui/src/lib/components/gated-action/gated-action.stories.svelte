<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import Button from '../button/button.svelte';
	import GatedAction from './gated-action.svelte';

	const { Story } = defineMeta({
		title: 'Components/GatedAction',
		component: GatedAction,
		tags: ['autodocs'],
		args: {
			allowed: false,
			action: 'New project',
			reason: 'estate-admin only (needs can_observe_events on the FGA root)',
		},
		argTypes: {
			allowed: { control: 'boolean' },
			reason: { control: 'text' },
			action: { control: 'text' },
		},
	});
</script>

<!-- The three controls are named rather than spread. `{...args}` would carry `children` (it is part of
     the component's prop type, so Storybook types it into args) and collide with the actual gated
     control below — "specified more than once, so this usage will be overwritten". -->

<!-- The refused state is the DEFAULT story on purpose: it is the one that has to be got right, and
     the one a reviewer should see first. `allowed` renders the child untouched, so an "allowed"
     story is really just a story about Button. -->
<Story name="Refused">
	{#snippet template(args)}
		<GatedAction allowed={args.allowed} reason={args.reason} action={args.action}>
			<Button variant="outline" size="sm">New project</Button>
		</GatedAction>
	{/snippet}
</Story>

<Story name="Allowed" args={{ allowed: true }}>
	{#snippet template(args)}
		<GatedAction allowed={args.allowed} reason={args.reason} action={args.action}>
			<Button variant="outline" size="sm">New project</Button>
		</GatedAction>
	{/snippet}
</Story>

<!-- Without `action`, the tooltip is the bare reason. Use this shape when the surrounding copy
     already names what is being refused and prefixing it would stutter. -->
<Story name="Reason only" args={{ action: undefined }}>
	{#snippet template(args)}
		<GatedAction allowed={args.allowed} reason={args.reason} action={args.action}>
			<Button variant="destructive" size="sm">Drop table</Button>
		</GatedAction>
	{/snippet}
</Story>

<!-- Geometry is unchanged by gating: the wrapper is `display: contents`, so a refused control sits
     in a row exactly where the allowed one would. All three buttons below should align. -->
<Story name="In a row, allowed vs refused">
	{#snippet template(args)}
		<div class="flex items-center gap-2">
			<Button variant="outline" size="sm">Refresh</Button>
			<GatedAction allowed={args.allowed} reason={args.reason} action={args.action}>
				<Button variant="outline" size="sm">New project</Button>
			</GatedAction>
			<Button variant="outline" size="sm">Export</Button>
		</div>
	{/snippet}
</Story>
