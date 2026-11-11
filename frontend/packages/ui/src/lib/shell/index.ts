// The shell's public surface: the frame every page renders in, the error page, and the types and
// matchers a sidebar is configured with. The rail, the bell and the breadcrumb are the frame's parts.
export { default as AppShell } from './app-shell.svelte';
export { default as AppError } from './app-error.svelte';
export * from './nav-config.js';
