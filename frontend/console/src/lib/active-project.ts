/**
 * The ACTIVE PROJECT — the project the console's pages are inside. Opening `/projects/<id>` enters it;
 * every page reads it back, so the shell can name the project you are in and route back to it.
 * A cookie (`path=/`, readable by script) because it carries a project id, not a secret.
 */
const ACTIVE_PROJECT_COOKIE = 'rask_active_project';

const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;

/** The active project, or `''` when none is open — a real state, not a loading one. */
export function readActiveProject(): string {
	for (const part of document.cookie.split(';')) {
		const [name, ...value] = part.trim().split('=');
		if (name === ACTIVE_PROJECT_COOKIE) return decodeURIComponent(value.join('='));
	}
	return '';
}

/** Enter a project. */
export function writeActiveProject(project: string): void {
	document.cookie = `${ACTIVE_PROJECT_COOKIE}=${encodeURIComponent(project)}; path=/; max-age=${ONE_YEAR_SECONDS}; samesite=lax`;
}
