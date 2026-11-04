/**
 * Sign-in, in the browser: OpenID Connect authorization code with PKCE against the platform's issuer
 * (Dex in development), as a public client. The console has no backend, so no secret exists to hold:
 * the browser proves possession of the code with the PKCE verifier and keeps the tokens IN MEMORY
 * (`InMemoryWebStorage`), never in `localStorage`. Only the transient sign-in state (the verifier and
 * the return path, for the length of one redirect) rides `sessionStorage`. A reload therefore starts
 * signed out and goes back to the issuer, which answers at once when it still holds a session.
 *
 * The issuer, client and scopes come from `/config.json`, which the console's release renders from
 * its values, so the built app carries no environment. Sign-in is OFF only where the config says so
 * (`{"signIn": false}`, an auth-off stack); a missing or malformed config is a configuration error the
 * console refuses to run past, never a quiet fall-back to signing nobody in.
 */
import { InMemoryWebStorage, User, UserManager, WebStorageStateStore } from 'oidc-client-ts';
import * as v from 'valibot';
import { setBearerProvider } from '@rask/api/client';

/** What the release renders into `/config.json`. `scopes` carries the lakehouse's audience scope
 *  (Dex's `audience:server:client_id:<audience>`), so the token's `aud` names the lakehouse and its
 *  doors accept it. */
const SignInConfigSchema = v.object({
	/** Absolute, or a path on this origin (`/dex`, when the Gateway serves the issuer same-origin). */
	issuer: v.pipe(v.string(), v.minLength(1)),
	clientId: v.pipe(v.string(), v.minLength(1)),
	scopes: v.array(v.string()),
});
/** The one other shape: a stack that signs nobody in says so. */
const SignInOffSchema = v.object({ signIn: v.literal(false) });
const ConsoleConfigSchema = v.union([SignInOffSchema, SignInConfigSchema]);
export type ConsoleConfig = v.InferOutput<typeof SignInConfigSchema>;

/** The signed-in person, as the console uses them. */
export interface Session {
	sub: string;
	name: string;
	email: string | null;
	accessToken: string;
	/** Epoch seconds; 0 when the token names no expiry. */
	expiresAt: number;
}

/** The two facts every page reads. */
class AuthState {
	/** Whether this stack signs people in at all. */
	enabled = $state(false);
	/** Why `/config.json` could not be used, when it could not: the console then runs nothing. */
	configError = $state<string | null>(null);
	session = $state.raw<Session | null>(null);
}

export const auth = new AuthState();

let manager: UserManager | null = null;
let ready: Promise<void> | null = null;

function toSession(user: User): Session {
	const claims = user.profile;
	const email = typeof claims.email === 'string' ? claims.email : null;
	return {
		sub: claims.sub,
		name: String(claims.name ?? claims.preferred_username ?? email ?? claims.sub),
		email,
		accessToken: user.access_token,
		expiresAt: user.expires_at ?? 0,
	};
}

type ConfigRead = { off: true } | { off: false; config: ConsoleConfig } | { error: string };

async function readConfig(): Promise<ConfigRead> {
	let body: unknown;
	try {
		const res = await fetch('/config.json', { headers: { accept: 'application/json' } });
		if (!res.ok) return { error: `/config.json answered ${res.status}` };
		body = await res.json();
	} catch (err) {
		return { error: `/config.json could not be read: ${String(err)}` };
	}
	const read = v.safeParse(ConsoleConfigSchema, body);
	if (!read.success) return { error: `/config.json is malformed: ${v.summarize(read.issues)}` };
	return 'signIn' in read.output ? { off: true } : { off: false, config: read.output };
}

/** Read the config once and build the OIDC client. Idempotent; every caller awaits the same read. */
export function initAuth(): Promise<void> {
	ready ??= (async () => {
		setBearerProvider(() => auth.session?.accessToken);
		const read = await readConfig();
		if ('error' in read) {
			auth.configError = read.error;
			return;
		}
		if (read.off) return;
		const config = read.config;
		manager = new UserManager({
			authority: new URL(config.issuer, location.origin).href.replace(/\/$/, ''),
			client_id: config.clientId,
			redirect_uri: `${location.origin}/auth/callback`,
			post_logout_redirect_uri: `${location.origin}/`,
			response_type: 'code',
			scope: config.scopes.join(' '),
			userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
			stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
			loadUserInfo: false,
			automaticSilentRenew: false,
			monitorSession: false,
		});
		auth.enabled = true;
	})();
	return ready;
}

/** Send the browser to the issuer; it comes back to `/auth/callback` and then to `returnTo`. */
export async function login(returnTo: string): Promise<void> {
	await initAuth();
	if (!manager) return;
	await manager.signinRedirect({ state: { returnTo } });
}

/** Finish the redirect: exchange the code (with the PKCE verifier) and hold the tokens in memory.
 *  Answers the path the person was on when they were sent to sign in. */
export async function completeLogin(): Promise<string> {
	await initAuth();
	if (!manager) return '/';
	const user = await manager.signinRedirectCallback();
	auth.session = toSession(user);
	const state = user.state as { returnTo?: unknown } | undefined;
	const returnTo = typeof state?.returnTo === 'string' ? state.returnTo : '/';
	// Same-origin paths only: the return path came through the issuer and is not ours to trust blindly.
	return returnTo.startsWith('/') && !returnTo.startsWith('//') ? returnTo : '/';
}

/** End the session here, and at the issuer when it advertises an end-session endpoint. */
export async function logout(): Promise<void> {
	await initAuth();
	auth.session = null;
	if (!manager) {
		location.assign('/');
		return;
	}
	await manager.removeUser();
	const metadata = await manager.metadataService.getMetadata().catch(() => null);
	if (metadata?.end_session_endpoint) {
		await manager.signoutRedirect();
		return;
	}
	location.assign('/');
}

/** Whether the held token has expired (a token with no expiry claim never does). */
export function expired(session: Session, nowSeconds = Math.floor(Date.now() / 1000)): boolean {
	return session.expiresAt > 0 && session.expiresAt <= nowSeconds;
}

/** The identity the shell's nav-user renders (name, email, initials). */
export interface SessionUser {
	name: string;
	email?: string;
	initials?: string;
}

/** Up-to-two-letter initials for the avatar fallback. */
function initialsOf(name: string): string {
	const words = name.trim().split(/\s+/).filter(Boolean);
	const letters =
		words.length >= 2 ? words[0]![0]! + words[1]![0]! : (words[0] ?? name).slice(0, 2);
	return letters.toUpperCase();
}

/** Project the session onto the shell identity, or `null` when signed out. */
export function sessionToUser(session: Session | null): SessionUser | null {
	if (!session) return null;
	const name = session.name || session.email || session.sub;
	const user: SessionUser = { name, initials: initialsOf(name) };
	if (session.email) user.email = session.email;
	return user;
}
