export { default as Dock } from './Dock.svelte';
export { raskDockTheme } from './theme';
export { DEFAULT_CHROME, EDGE_DROP, resolveChrome } from './chrome';
export type { DockChrome, DockChromeOptions } from './chrome';
// `PanelProps.alert` is the whole public surface for watchers — `DockAlerts` and `PanelAlert` stay
// internal, so a zone can never acquire a record for a panel it does not own.
export { default as ViewSidebar } from './ViewSidebar.svelte';
export { DockViews } from './views.svelte';
export type { ViewSummary, ViewsPhase } from './views.svelte';
export { NOOP_ALERT } from './alerts.svelte';
export type { AlertDetail, AlertTone, PanelAlertApi, StopWatching } from './alerts.svelte';
export type {
	LayoutRead,
	LayoutStore,
	PanelComponent,
	PanelEntry,
	PanelIcon,
	PanelProps,
	PanelRegistry,
} from './types';
