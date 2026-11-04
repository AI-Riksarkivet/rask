import type { PageLoad } from './$types';

// The shell names the project in the URL. The load has no side effect: `app.html` preloads on hover,
// so the page component, not this load, stamps the active project once the page is shown.
export const load: PageLoad = ({ params }) => ({ activeProject: params.project });
