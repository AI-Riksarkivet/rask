/**
 * The lineage workbench's context pair — one `LineageState`, polled once, read by every panel.
 *
 * `createContext` rather than a hand-rolled `Symbol` + `getContext` + throw: the Svelte docs prefer it
 * because it is type-safe and "makes it unnecessary to use keys", and it throws its own error when a
 * consumer is mounted outside a provider. Available since 5.40; this workspace is on 5.56.
 *
 * It works across the dock boundary because `<Dock>` captures its own context tree with
 * `getAllContexts()` and hands it to every panel mount. So the graph, the run board and the event feed
 * share one store and can never be a poll apart, and a panel dragged into a floating group keeps
 * reading the same store it always did.
 */
import { createContext } from 'svelte';
import type { LineageState } from './store.svelte';

export const [getLineageState, setLineageState] = createContext<LineageState>();
