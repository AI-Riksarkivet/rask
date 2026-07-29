/**
 * The media workbench's context pair.
 *
 * `createContext` rather than a hand-rolled `Symbol` + `getContext` + throw: the Svelte docs prefer it
 * because it is type-safe and "makes it unnecessary to use keys", and it throws its own error when a
 * consumer is mounted outside a provider. Available since 5.40; this workspace is on 5.56.
 *
 * It works across the dock boundary because `<Dock>` captures its own context tree with
 * `getAllContexts()` and hands it to every panel mount — so calling `setMediaWorkbench(...)` anywhere
 * above the dock is enough, exactly as if the panels were rendered in that subtree.
 */
import { createContext } from 'svelte';
import type { MediaWorkbench } from '$lib/dock/workbench.svelte';

export const [getMediaWorkbench, setMediaWorkbench] = createContext<MediaWorkbench>();
