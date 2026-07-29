"""Governed demo for the kind deployment — Dex (OIDC) → catalog → OpenFGA, end to end.

Runs INSIDE the catalog pod (so Dex + the catalog resolve at their in-cluster names). Pipe it in:

    kubectl exec -i deploy/lance-ns-catalog -c catalog -- python - < scripts/governed_demo_k8s.py

Proves: Dex issues real id_tokens (password grant); the catalog rejects unauthenticated calls (401);
OpenFGA authorizes — the creator (alice) owns what she made (200), a different identity (bob) is denied
(403). Requires the stack deployed with `--set auth.enabled=true`.
"""

from __future__ import annotations

import time

import httpx


DEX = "http://lance-ns-dex:5556/dex"
CATALOG = "http://localhost:2333"


def token(username: str) -> str:
    """A real Dex id_token via the OIDC password grant."""
    resp = httpx.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "lance-catalog",
            "client_secret": "lance-catalog-secret",
            "scope": "openid email",
            "username": username,
            "password": "password",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id_token"]


def bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def main() -> None:
    alice, bob = token("alice@example.com"), token("bob@example.com")
    print("1. Dex issued id_tokens for alice + bob (OIDC password grant)")

    ns = f"govdemo{int(time.time()) % 100000}"
    created = httpx.post(f"{CATALOG}/v1/namespace/{ns}/create", json={}, headers=bearer(alice), timeout=10)
    print(f"2. alice creates namespace {ns!r} (authn + becomes owner): {created.status_code}")

    no_auth = httpx.post(f"{CATALOG}/v1/namespace/{ns}x/create", json={}, timeout=10)
    print(f"3. same call with NO token → rejected: {no_auth.status_code} (expect 401)")

    a = httpx.post(f"{CATALOG}/v1/namespace/{ns}/describe", headers=bearer(alice), timeout=10)
    b = httpx.post(f"{CATALOG}/v1/namespace/{ns}/describe", headers=bearer(bob), timeout=10)
    print(f"4. OpenFGA authz — alice (owner): {a.status_code} (200) | bob (no grant): {b.status_code} (403)")
    print("✓ Dex → catalog → OpenFGA governed flow verified" if (a.status_code, b.status_code) == (200, 403) else "✗ unexpected authz result")


if __name__ == "__main__":
    main()
