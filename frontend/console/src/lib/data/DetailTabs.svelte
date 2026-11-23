<script lang="ts">
	// The detail-page tab bar (goal cond 3): the design system's tab strip, with the panels left to the
	// page (it renders the active one itself). Overview stays the default; Access hosts the FGA view.
	// State lives in the page (bindable), so a route can reset it on id change. A page that keeps the
	// active tab in the URL passes `onselect` instead and owns the transition: binding AND writing the
	// URL from an effect races on entry (the effect order decides whether a `?tab=` deep link survives).
	import * as Tabs from '@riksarkivet/ui/tabs';

	let {
		tabs,
		active = $bindable(),
		onselect,
	}: { tabs: string[]; active: string; onselect?: (tab: string) => void } = $props();
</script>

<Tabs.Root
	class="mb-4"
	value={active}
	onValueChange={(tab) => (onselect ? onselect(tab) : (active = tab))}
>
	<Tabs.List>
		{#each tabs as t (t)}
			<Tabs.Trigger value={t}>{t}</Tabs.Trigger>
		{/each}
	</Tabs.List>
</Tabs.Root>
