<script lang="ts">
	import { page } from '$app/state';
	import { base } from '$app/paths';
	import { HardDrive, Layers } from '@lucide/svelte';
	import { buttonVariants } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';

	let { children } = $props();

	// Two views of ONE thing — the storage registry. Browse walks objects inside a store; By tier
	// answers "which store backs silver?". They were two sidebar entries, which made them read as two
	// unrelated areas and put a second-level rail item next to top-level ones. Tabs say what is true:
	// same subject, different lens.
	//
	// LINKS, not a JS tab component. Each view keeps its own URL, so a tier view is shareable and
	// survives reload; @rask/ui ships no `tabs` export, and a client-state toggle would trade the URL
	// away for nothing. `aria-current` carries the state a `<Tabs>` role would.
	const TABS = [
		{ href: `${base}/catalog/storage`, label: 'Browse', icon: HardDrive, exact: true },
		{ href: `${base}/catalog/storage/tiers`, label: 'By tier', icon: Layers, exact: false },
	];

	const active = (t: (typeof TABS)[number]) =>
		t.exact ? page.url.pathname === t.href : page.url.pathname.startsWith(t.href);
</script>

<nav aria-label="Storage views" class="border-border mb-4 flex gap-1 border-b pb-2">
	{#each TABS as tab (tab.href)}
		{@const on = active(tab)}
		<a
			href={tab.href}
			aria-current={on ? 'page' : undefined}
			class={cn(
	buttonVariants({ variant: 'ghost', size: 'sm' }),
	'gap-2',
	on && 'bg-muted text-foreground font-medium',
)}
		>
			<tab.icon class="size-4" aria-hidden="true" />
			{tab.label}
		</a>
	{/each}
</nav>

{@render children?.()}
