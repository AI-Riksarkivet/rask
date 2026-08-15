<script lang="ts">
	import type { Snippet } from 'svelte';
	import { on } from 'svelte/events';
	import * as Tooltip from '../tooltip/index.js';
	import { cn } from '../../utils/cn.js';

	// GATED ACTION — the owner ruling (#143): "every gated action SHOWS disabled with its denial
	// reason, never hidden."
	//
	// Hiding an action a user cannot take teaches them the capability does not EXIST. They then ask
	// for a feature that is already there, or conclude the product cannot do it. Showing it disabled,
	// with the reason attached, turns a dead end into a request they can make of whoever can grant it —
	// which in this estate is a real, reachable person, because the reason names the relation and the
	// object (`<subject> lacks <relation> on <object>`, the FGA denial's own wording).
	//
	// TWO THINGS MAKE THIS NON-TRIVIAL, and both are why the pattern belongs here rather than being
	// re-typed at every call site:
	//
	// 1. A `disabled` button fires NO pointer events, so a tooltip on it never opens — the reason
	//    would be attached to something that can never show it. This uses `aria-disabled` plus a
	//    capturing click/keydown guard instead: the control still receives hover and focus (so the
	//    reason is reachable by mouse AND keyboard), while the action genuinely cannot fire.
	// 2. It stays FOCUSABLE on purpose. `disabled` removes a control from the tab order, which hides
	//    the explanation from exactly the users who most need it announced.
	//
	// The guard is capture-phase and calls `stopImmediatePropagation`, so a handler bound directly on
	// the child never runs — a bubble-phase guard would fire after it.

	interface Props {
		/** Whether the caller is allowed. `true` renders the child untouched. */
		allowed: boolean;
		/** WHY it is refused, in the denial's own words. Shown on hover and focus, and announced. */
		reason: string;
		/** Optional label for what is being refused, prepended to the tooltip. */
		action?: string;
		class?: string;
		children: Snippet;
	}

	let { allowed, reason, action, class: className, children }: Props = $props();

	const message = $derived(action ? `${action} — ${reason}` : reason);

	function block(node: HTMLElement) {
		const stop = (event: Event) => {
			if (allowed) return;
			event.preventDefault();
			event.stopImmediatePropagation();
		};
		// `on` from `svelte/events`, not `addEventListener`: it preserves Svelte's own delegated-handler
		// ordering, so a child's `onclick=` cannot slip in ahead of this guard, and it returns its own
		// unsubscriber. `{ capture: true }` is the load-bearing half either way.
		const offClick = on(node, 'click', stop, { capture: true });
		const offKey = on(
			node,
			'keydown',
			(event) => {
				if (event.key === 'Enter' || event.key === ' ') stop(event);
			},
			{ capture: true },
		);
		return () => {
			offClick();
			offKey();
		};
	}
</script>

{#if allowed}
	{@render children()}
{:else}
	<Tooltip.Provider>
		<Tooltip.Root>
			<!-- The `child` snippet, NOT the default trigger. `Tooltip.Trigger` renders a <button> of its
			     own, and the thing being gated is almost always a <button> too — nesting them is invalid
			     HTML, so the browser repairs it by hoisting the inner one out, content shifts, and SSR
			     warns `node_invalid_placement_ssr` followed by a hydration mismatch. (Observed, not
			     theorised: the home settings suite printed exactly that before this snippet existed.)
			     Delegating lets the trigger BE this span, so there is one element and it is not a button. -->
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<!-- `contents` keeps this wrapper out of the layout: the child keeps whatever geometry its
					     own parent gave it, so gating a control never nudges the row it sits in. -->
					<span
						{...props}
						data-slot="gated-action"
						class={cn('contents [&_*]:cursor-not-allowed [&_*]:opacity-50', className)}
						aria-disabled="true"
						{@attach block}
					>
						{@render children()}
					</span>
				{/snippet}
			</Tooltip.Trigger>
			<!-- `role="status"`: the reason is an explanation of the current state, not an alert. -->
			<Tooltip.Content role="status" class="max-w-xs text-xs">{message}</Tooltip.Content>
		</Tooltip.Root>
	</Tooltip.Provider>
{/if}
