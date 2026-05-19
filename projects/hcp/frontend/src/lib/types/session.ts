/** Client-safe tenant session metadata (no server imports needed) */
export interface TenantSession {
  tenant: string | undefined;
  username: string;
  exp: number;
  expired: boolean;
  cookieName: string;
  isActive: boolean;
}
