import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import type { PageLoad } from './$types';

// /compute → /compute/overview (the compute landing page).
export const load: PageLoad = () => {
	redirect(307, `${base}/overview`);
};
