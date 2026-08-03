/**
 * The page↔layout bridge for the workbench's rail content. The saved-views list renders inside the
 * SHARED shell rail (AppShell's sidebarContent — so this zone has the same project switcher,
 * breadcrumbs and collapse behaviour as every other zone), but the views model is built by the
 * PAGE, which owns the dock api. A tiny rune singleton carries the instance across; the layout
 * renders it only in the browser, so the server never shares state across requests (module scope
 * is per-request-pool on the server — nothing user-specific may live here during SSR).
 */
import type { SerializedDockview } from 'dockview';
import type { DockViews } from '@rask/dockview/views';

class Bench {
	views = $state<DockViews<SerializedDockview> | null>(null);
	apply = $state<((id: string) => void) | null>(null);
}

export const bench = new Bench();
