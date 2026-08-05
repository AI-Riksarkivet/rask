import type { LayoutServerLoad } from './$types';
import { zoneLayoutLoad } from '@rask/api/bff';

// The estate-wide zone layout contract (identity + auth flag + the ACTIVE PROJECT, #103) —
// single-sourced in @rask/api/bff so every zone derives the same shell context.
export const load: LayoutServerLoad = zoneLayoutLoad;
