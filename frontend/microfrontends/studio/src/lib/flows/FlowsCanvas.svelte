<script lang="ts">
	/** The canvas column: the flow pane above, the bottom drawer below.
	 *
	 *  The provider wraps ONLY the pane, because `useSvelteFlow()` is the pane's need (drag-drop and
	 *  click-to-add positioning) and the drawer has no business in flow context. Raw `<SvelteFlow>`
	 *  rather than `@rask/flow`'s `GraphCanvas`: that one is the read-only viewer and its docstring
	 *  rules out growing it an editor mode — this zone takes styles.css and `layout` from there. */
	import { SvelteFlowProvider } from '@xyflow/svelte';
	import FlowPane from '$lib/flows/FlowPane.svelte';
	import BottomDrawer from '$lib/flows/BottomDrawer.svelte';
</script>

<div class="flex h-full min-h-0 w-full flex-col">
	<div class="min-h-0 flex-1">
		<SvelteFlowProvider>
			<FlowPane />
		</SvelteFlowProvider>
	</div>
	<BottomDrawer />
</div>
