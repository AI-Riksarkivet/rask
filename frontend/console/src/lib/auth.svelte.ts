/**
 * Sign-in, in the browser: OpenID Connect authorization code with PKCE against the platform's issuer
 * (Dex in development), as a public client. The console has no backend, so no secret exists to hold:
 * the browser proves possession of the code with the PKCE verifier and keeps the tokens IN MEMORY
 * (`InMemoryWebStorage`), never in `localStorage`. Only the transient sign-in state (the verifier and
 * the return path, for the length of one redirect) rides `sessionStorage`. A reload therefore starts
 * signed out and goes back to the issuer: one that keeps a session answers at once, and the dev Dex,
 * which keeps none, shows its login form.
 *
 * The issuer, client and scopes come from `/config.json`, which the console's release renders from
 * its values, so the built app carries no environment. Sign-in is OFF only where the config says so
 * (`{"signIn": false}`, an auth-off stack); a missing or malformed config is a configuration error the
 * console refuses to run past, never a quiet fall-back to signing nobody in.
 */
import { InMemoryWebStorage, User, UserManager, WebStorageStateStore } from 'oidc-client-ts';
import * as v from 'valibot';
import { setBearerProvider } from '@rask/api/client';
import { sameOriginPath } from './return-path.ts';

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

/** The sign-in route's parameter that asks the issuer to re-authenticate, `/auth/login?prompt=login`.
 *  `login` is the one value it carries: OIDC Core's `prompt=login` makes an issuer that keeps a session
 *  authenticate the person again instead of answering from it. Dex reads `prompt` only with its
 *  sessions on (`server/authflow/login.go`, behind `DEX_SESSIONS_ENABLED`); the dev Dex keeps no
 *  session and authenticates every sign-in anyway. */
export const REAUTHENTICATE = { param: 'prompt', value: 'login' } as const;

/** Send the browser to the issuer; it comes back to `/auth/callback` and then to `returnTo`.
 *  `reauthenticate` sends `prompt=login`, so a session an issuer still holds signs nobody in. */
export async function login(returnTo: string, { reauthenticate = false } = {}): Promise<void> {
	await initAuth();
	if (!manager) return;
	await manager.signinRedirect({
		state: { returnTo },
		...(reauthenticate ? { prompt: REAUTHENTICATE.value } : {}),
	});
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
	return sameOriginPath(returnTo);
}

/** End the session here, and at the issuer when it advertises an end-session endpoint.
 *
 *  The user stays held until the redirect is built: `signoutRedirect` reads the held user's id token
 *  to send as `id_token_hint` (the issuer's proof of which session to end) and removes the user
 *  itself before it navigates. Removing it first would send the request without the hint.
 *
 *  An issuer that advertises no end-session endpoint offers no way to end its session from here, and
 *  one that keeps a session would sign the person straight back in on the next visit. So the browser
 *  goes to the sign-in route with `prompt=login`: an issuer that keeps a session must authenticate
 *  whoever comes next, and a session left behind on a shared machine signs nobody in. Dex advertises
 *  its end-session endpoint exactly when its sessions are on (`server/discovery/discovery.go`), so the
 *  dev Dex, whose sessions are off, takes this path and keeps no session to reuse. Nothing is stored
 *  to carry that intent; it rides the one redirect's URL. */
export async function logout(): Promise<void> {
	await initAuth();
	auth.session = null;
	if (!manager) {
		location.assign('/');
		return;
	}
	const metadata = await manager.metadataService.getMetadata().catch(() => null);
	if (metadata?.end_session_endpoint) {
		await manager.signoutRedirect();
		return;
	}
	await manager.removeUser();
	const next = new URLSearchParams({ [REAUTHENTICATE.param]: REAUTHENTICATE.value });
	location.assign(`/auth/login?${next}`);
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
