import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

// The project overview (batch dashboard) was carved out into the `overview-frontend`
// microfrontend (served at /<project>/overview). A bare /<project> has no page of its
// own here, so land the operator on the overview — the project's home surface.
export const load: PageLoad = ({ params }) => {
	redirect(307, `/${params.project}/overview`);
};
