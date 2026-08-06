// GENUINELY client-only: the flow graph is a module-level runes singleton
// hydrated from localStorage, and SSR-rendering it would share one instance
// across requests + guarantee a hydration divergence (the reference editor in
// the explorer zone has exactly that drift — its page claims ssr=false in a
// comment while actually SSRing). The canvas is an editor, not content; there
// is nothing for a crawler or first-paint here.
export const ssr = false;
