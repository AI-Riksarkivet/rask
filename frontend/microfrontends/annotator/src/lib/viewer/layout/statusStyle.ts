import type { BadgeVariant } from '@rask/ui/badge';

/** Shared status → style map for the review surfaces (list, table, detail). Kept tiny and in
 *  one place so the dot and the badge never disagree, and expressed in the estate's SEMANTIC
 *  tokens (success / warning / destructive / primary) rather than the bespoke emerald-rose-amber
 *  ramp this zone used to carry — the tokens already carry a correct value in both themes. */
export function statusDot(status: string): string {
	switch (status) {
		case 'accepted':
			return 'bg-success';
		case 'rejected':
			return 'bg-destructive';
		case 'prediction':
			return 'bg-warning';
		case 'reviewed':
			return 'bg-primary';
		default:
			return 'bg-muted-foreground';
	}
}

/** The @rask/ui Badge variant for a status — the same badge vocabulary the data and admin
 *  zones use, so a status chip here reads as the same object it does over there. */
export function statusVariant(status: string): BadgeVariant {
	switch (status) {
		case 'accepted':
			return 'success';
		case 'rejected':
			return 'destructive';
		case 'prediction':
			return 'warning';
		case 'reviewed':
			return 'default';
		default:
			return 'outline';
	}
}
