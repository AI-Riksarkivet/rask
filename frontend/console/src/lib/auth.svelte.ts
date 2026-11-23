/**
 * Sign-in, in the browser: OpenID Connect authorization code with PKCE against the platform's issuer
 * (Dex in development), as a public client. The console has no backend, so no secret exists to hold:
 * the browser proves possession of the code with the PKCE verifier and keeps the tokens IN MEMORY
 * (`InMemoryWebStorage`), never in `localStorage`. Only the transient sign-in state (the verifier and
 * the return path, for the length of one redirect) rides `sessionStorage`. A reload therefore starts
 * signed out and goes back to the issuer, and one that keeps a session answers at once.
 *
 * THE SESSION IS RENEWED WITHOUT LEAVING THE PAGE where the issuer grants a refresh token (the
 * `offline_access` scope, which the release's scopes carry): shortly before the access token expires, and
 * when a door answers 401, the refresh token is exchanged for a new access token, in memory like the first.
 * Where it cannot be renewed (no refresh token, or the issuer refuses it) the session has ENDED: the page
 * stays as it is, says so, and the person chooses when to sign in again, so nothing they typed is thrown
 * away by a redirect they did not ask for. The renewal is never an iframe: the console's own policy
 * forbids framing it, and a third-party cookie an iframe would need is not something to rely on.
 *
 * The issuer, client and scopes come from `/config.json`, which the console's release renders from
 * its values, so the built app carries no environment. Sign-in is OFF only where the config says so
 * (`{"signIn": false}`, an auth-off stack); a missing or malformed config is a configuration error the
 * console refuses to run past, never a quiet fall-back to signing nobody in.
 */
import { InMemoryWebStorage, User, UserManager, WebStorageStateStore } from 'oidc-client-ts';
import * as v from 'valibot';
import { setBearerProvider, setBearerRenewal } from '#lib/api/client.ts';
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
	/** The session could not be renewed: its token expired or a door refused it, and the issuer gave no
	 *  new one. The page says so and offers to sign in again. */
	ended = $state(false);
	/** The issuer and the scopes the release names, for what the console tells a client to sign in
	 *  with; null and none on a stack that signs nobody in. */
	issuer = $state<string | null>(null);
	scopes = $state.raw<readonly string[]>([]);
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

async function readConfig(fetch: typeof globalThis.fetch): Promise<ConfigRead> {
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

/** Read the config once and build the OIDC client. Idempotent; every caller awaits the same read.
 *  The root layout's `load` calls it first, with its own `fetch`; a later caller's `fetch` is unused. */
export function initAuth(fetch: typeof globalThis.fetch = globalThis.fetch): Promise<void> {
	ready ??= (async () => {
		setBearerProvider(() => auth.session?.accessToken);
		setBearerRenewal(() => renew({ refused: true }));
		const read = await readConfig(fetch);
		if ('error' in read) {
			auth.configError = read.error;
			return;
		}
		if (read.off) return;
		const config = read.config;
		const authority = new URL(config.issuer, location.origin).href.replace(/\/$/, '');
		manager = new UserManager({
			authority,
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
		manager.events.addUserLoaded((user) => {
			if (signingOut) return;
			auth.session = toSession(user);
			auth.ended = false;
		});
		manager.events.addAccessTokenExpiring(() => void renew());
		auth.issuer = authority;
		auth.scopes = config.scopes;
		auth.enabled = true;
	})();
	return ready;
}

/** Set by `logout`: a renewal that settles while the browser is leaving signs nobody back in. */
let signingOut = false;
let renewing: Promise<boolean> | null = null;
/** Whether a caller of the exchange in flight came from a door's 401. */
let doorRefused = false;

/** Exchange the refresh token for a new access token, without leaving the page. Answers whether the
 *  session now holds a new one. Callers that arrive while an exchange is in flight share its answer, so
 *  a burst of 401s spends one refresh token.
 *
 *  A session that could not be renewed has ENDED once its token is of no more use: a door refused it
 *  (`refused`), or it has expired. An early renewal that fails while the token is still good ends
 *  nothing; the expiry's own attempt decides. */
export function renew({ refused = false } = {}): Promise<boolean> {
	// An ended session stays ended until the person signs in again: asking the issuer once more, on every
	// poll a page makes, would only be refused once more.
	if (auth.ended || auth.session === null) return Promise.resolve(false);
	doorRefused ||= refused;
	renewing ??= (async () => {
		try {
			const held = await manager?.getUser();
			if (!manager || !held?.refresh_token) return false;
			const user = await manager.signinSilent();
			// Signed out while the exchange was in flight: the answer is nobody's session.
			if (!user || auth.session === null) return false;
			auth.session = toSession(user);
			return true;
		} catch {
			return false;
		}
	})().then((renewed) => {
		const useless = doorRefused || (auth.session !== null && expired(auth.session));
		renewing = null;
		doorRefused = false;
		if (renewed) auth.ended = false;
		else if (auth.session !== null && useless) auth.ended = true;
		return renewed;
	});
	return renewing;
}

/** The sign-in route's parameter that asks the issuer to re-authenticate, `/auth/login?prompt=login`.
 *  `login` is the one value it carries: OIDC Core's `prompt=login` makes an issuer that keeps a session
 *  authenticate the person again instead of answering from it. Dex reads `prompt` only with its
 *  sessions on (`server/authflow/login.go`, behind `DEX_SESSIONS_ENABLED`), as the dev Dex's are. */
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
	auth.ended = false;
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
 *  its end-session endpoint exactly when its sessions are on (`server/discovery/discovery.go`). Nothing
 *  is stored to carry that intent; it rides the one redirect's URL. */
export async function logout(): Promise<void> {
	await initAuth();
	signingOut = true;
	auth.session = null;
	auth.ended = false;
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
