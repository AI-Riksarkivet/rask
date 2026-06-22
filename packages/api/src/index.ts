// @rask/api — shared frontend data layer (API client + types), split by domain.
// JIT TS: apps import the source directly (Vite/svelte-check transpile it) — no build.
// Server-only bits (remote functions, $env) stay per-app.
export * from './ray';
export * from './batches';
export * from './search';
export * from './volumes';
export * from './types';
