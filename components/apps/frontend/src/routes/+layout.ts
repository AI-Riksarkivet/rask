// Pure SPA: no SSR, no prerendering of dynamic routes.
// adapter-static with `fallback: 'index.html'` lets the SPA handle all routes client-side.
export const ssr = false;
export const prerender = false;
