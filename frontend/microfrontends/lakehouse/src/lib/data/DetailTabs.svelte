<script lang="ts">
	// The detail-page tab bar (goal cond 3): a thin, accessible tablist — Overview stays the
	// default; Access hosts the FGA view. State lives in the page (bindable), so a route can reset
	// it on id change. A page that keeps the active tab in the URL passes `onselect` instead and
	// owns the transition — binding AND writing the URL from an effect races on entry (the effect
	// order decides whether a `?tab=` deep link survives).
	let {
		tabs,
		active = $bindable(),
		onselect,
	}: { tabs: string[]; active: string; onselect?: (tab: string) => void } = $props();
</script>

<div class="tabs" role="tablist">
	{#each tabs as t (t)}
		<button
			type="button"
			role="tab"
			aria-selected={active === t}
			class:active={active === t}
			onclick={() => (onselect ? onselect(t) : (active = t))}
		>
			{t}
		</button>
	{/each}
</div>

<style>
	.tabs {
		display: flex;
		gap: 2px;
		border-bottom: 1px solid var(--line);
		margin-bottom: 18px;
	}
	button {
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--mut);
		font-size: 13px;
		padding: 6px 12px;
		cursor: pointer;
		text-transform: capitalize;
	}
	button:hover {
		color: var(--ink);
	}
	button.active {
		color: var(--ink);
		border-bottom-color: var(--accent, #ffc14d);
	}
</style>
