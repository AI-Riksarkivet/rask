/**
 * Clear the origin-wide session cookie and return home (home zone owns /auth/*). Accept POST (the
 * form-driven path, CSRF-safer — the shell's Sign out is a form) and GET (a plain link). Deleting at
 * path "/" signs the user out of every path-routed zone at once.
 */
import { redirect, type RequestHandler } from '@sveltejs/kit';
import { SESSION_COOKIE } from '@rask/api/oidc';

const signOut: RequestHandler = ({ cookies }) => {
	cookies.delete(SESSION_COOKIE, { path: '/' });
	redirect(302, '/');
};

export const POST = signOut;
export const GET = signOut;
