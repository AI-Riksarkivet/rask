"""A stand-in ServiceAccount issuer for a Dagger lane's throwaway RustFS, so a lane boots the catalog as the chart does.

The chart's catalog holds no store key: it exchanges its projected ServiceAccount token at the store's STS
(`AssumeRoleWithWebIdentity`) for a session under the canned policy the token's subject names (ADR 0170). A lane has no
Kubernetes to project that token, so this script stands in for the cluster's issuer ([[XC-081]]):

    python scripts/lane_store_issuer.py key <state-dir>                                   # an RSA key, made once per run
    python scripts/lane_store_issuer.py serve <port> <public-url> <issuer> <state-dir>            # discovery and the key set
    python scripts/lane_store_issuer.py mint <state-dir> <issuer> <subject> <audience> <ttl> <out> # a token as k3s projects
    helm template ... | python scripts/lane_store_issuer.py policy <component>            # a component's canned policy

`serve` publishes what rask's issuer mirror publishes (`lakehouse.governed.issuer_mirror`): the discovery document of an
issuer whose `iss` is `<issuer>` (the lane passes the cluster's, the one name the store and the token must share), its
`jwks_uri` at `<public-url>`, and the key set; the lane's store trusts it as its
workload provider, as the rustfs fixture's does the mirror (deploy/fixtures/rustfs). `policy` reads the canned policy off
the chart's own render (the `<release>-object-store-identities` ConfigMap), so a lane grants exactly what the chart does.
The key lives only in the lane's run and signs nothing else.
"""

import argparse
import base64
import http.server
import json
import sys
import time
import uuid
from pathlib import Path

import jwt
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


_KID = "lane"


def _key(state: Path) -> rsa.RSAPrivateKey:
    loaded = serialization.load_pem_private_key((state / "issuer.pem").read_bytes(), password=None)
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise TypeError(f"{state / 'issuer.pem'} holds no RSA key")
    return loaded


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def make_key(state: Path) -> None:
    """Write a fresh RSA key to ``state``."""
    state.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (state / "issuer.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def serve(port: int, public: str, issuer: str, state: Path) -> None:
    """Serve the discovery document and the key set until killed."""
    numbers = _key(state).public_key().public_numbers()
    documents = {
        "/.well-known/openid-configuration": {
            "issuer": issuer,
            "jwks_uri": f"{public.rstrip('/')}/openid/v1/jwks",
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        },
        "/openid/v1/jwks": {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": _KID, "n": _b64(numbers.n), "e": _b64(numbers.e)}]},
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the stdlib's name
            body = documents.get(self.path)
            payload = json.dumps(body).encode() if body is not None else b"{}"
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def mint(state: Path, issuer: str, subject: str, audience: str, ttl: int, out: Path) -> None:
    """Write a token ``issuer`` signs for ``subject`` at ``audience``, living ``ttl`` seconds, in the claims k3s projects."""
    now = int(time.time())
    namespace, name = subject.split(":")[2:4]
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": [audience],
        "exp": now + ttl,
        "iat": now - 5,
        "nbf": now - 5,
        "jti": str(uuid.uuid4()),
        "kubernetes.io": {"namespace": namespace, "serviceaccount": {"name": name, "uid": str(uuid.uuid4())}},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(jwt.encode(claims, _key(state), algorithm="RS256", headers={"kid": _KID}), encoding="utf-8")


def policy(component: str, rendered: str) -> str:
    """The canned policy the chart grants ``component`` (`rask-sa-<component>.json`), compact, off its render."""
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "ConfigMap" and str(doc["metadata"]["name"]).endswith("-object-store-identities"):
            for key, document in (doc.get("data") or {}).items():
                if key.endswith(f"-sa-{component}.json"):
                    return json.dumps(json.loads(document), separators=(",", ":"))
    raise SystemExit(f"the render grants {component!r} no store policy")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key").add_argument("state", type=Path)
    served = commands.add_parser("serve")
    served.add_argument("port", type=int)
    served.add_argument("public")
    served.add_argument("issuer")
    served.add_argument("state", type=Path)
    minted = commands.add_parser("mint")
    minted.add_argument("state", type=Path)
    minted.add_argument("issuer")
    minted.add_argument("subject")
    minted.add_argument("audience")
    minted.add_argument("ttl", type=int)
    minted.add_argument("out", type=Path)
    commands.add_parser("policy").add_argument("component")
    args = parser.parse_args()
    match args.command:
        case "key":
            make_key(args.state)
        case "serve":
            serve(args.port, args.public, args.issuer, args.state)
        case "mint":
            mint(args.state, args.issuer, args.subject, args.audience, args.ttl, args.out)
        case "policy":
            print(policy(args.component, sys.stdin.read()))


if __name__ == "__main__":
    main()
