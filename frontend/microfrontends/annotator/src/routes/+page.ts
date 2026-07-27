// Per-page SSR opt-out (the app itself is server-aware — hooks + BFF routes run on
// every request): this route IS the Pixi canvas shell. The engine (PixiCanvas /
// InteractionManager / WebGL context) and the temporal viewers (waveform, video
// frame-overlay) are browser-only by nature — server-rendering the shell would mean
// teaching the whole engine import graph to no-op off-DOM for zero useful paint (the
// canvas is the page). The layout (navbar, session) still hydrates from the server.
export const ssr = false;
