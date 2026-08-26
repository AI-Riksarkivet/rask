#!/usr/bin/env python
"""Create the medallion cascade's TOP-LEVEL namespaces through the only door that can.

    POST /v1/warehouses/{id}/namespaces

WHY THIS EXISTS. The cascade creates its own namespaces on the way down — but only the nested ones.
``medallion/services/catalog_register.py`` ensures a parent before registering a table and then
deliberately stops short of the top:

    parent = delimiter.join(parent_segments) if len(parent_segments) > 1 else ""

For ``silver-media$features`` the parent segments are ``["silver-media"]`` — length 1 — so ``parent``
is empty and no create is attempted. That is correct and intentional: a top-level namespace binds to
a WAREHOUSE, ``require_warehouse_scoped`` refuses to make one from the table door, and the register
call's own error is meant to say so rather than paper over it.

What was missing is the other half. ``scripts/seed_medallion_fga.sh`` links every cascade namespace
under the warehouse in OpenFGA — ``bronze``, ``silver``, ``gold``, ``bronze-media``, ``silver-media``
— so authorization passes for a namespace the CATALOG has never heard of. Measured live 2026-08-25:
the media lane's mover cleared its ``can_create_table`` check and then died on

    404 ... silver-media$features/create

which reads as a broken lane rather than an unprovisioned one. Authorization and existence were
seeded by different files and only one of them ran.

AGNOSTIC BY CONSTRUCTION. The namespaces are read out of ``chart/values.yaml`` — the producer's
``bronzeNamespace`` plus every mover's ``fromNamespace``/``toNamespace`` — so this names no lane and
no workload. Add a mover to the chart and this seeds its namespaces; there is nothing here to edit.

THE WAREHOUSE IS REQUIRED, AND IT IS NOT THE BUCKET. Measured 2026-08-25, after this script was
first written with ``--warehouse`` defaulting to ``lance-catalog``: that string is the S3 BUCKET and
the OpenFGA STORE name, and it is never a warehouse id. Three different objects share it —

* the S3 bucket ``lance-catalog`` (RustFS Tenant + the ``rustfs-mkbucket`` Job), which is the
  catalog's shared default root, ``LANCE_REST_ROOT``;
* the OpenFGA store ``lance-catalog`` (``fga.provision(..., store_name="lance-catalog")``);
* the FGA root OBJECT ``warehouse:lance_catalog`` (underscore) — a synthetic authorization anchor
  with no registry record, and correct as written: the catalog's id regex never applies to it.

A warehouse named ``lance-catalog`` cannot be created at all: the catalog root bucket is reserved
platform storage (``Settings.reserved_bucket_set``) and ``POST /v1/warehouses`` refuses it 400. So the
old default produced five 403s that read as a permissions problem and were a naming one.

BEWARE THE CONTRADICTION THIS SCRIPT CANNOT FIX. With ``catalog.warehouses.enabled: true`` (the chart
DEFAULT), ``require_warehouse_scoped`` makes every top-level namespace belong to a warehouse; but the
chart also leaves ``medallion.buckets: {}``, so every stage falls back to ``rustfs.bucket`` — the
reserved root, which no warehouse may back. On that configuration the cascade's namespaces are
unprovisionable by construction and no ``--warehouse`` value is correct. ``bronze`` and ``silver``
exist on the live estate only because they predate the guard. Resolving it is a topology decision
(give the medallion its own buckets, or run single-bucket with warehouses off) — not something to
paper over here, which is why this script reports and exits non-zero rather than guessing.

AUTH. The catalog accepts ONLY IdP bearers when auth is on, and this is a governed write. Same
convention as ``scripts/seed_estate.py``:

    SEED_CATALOG_TOKEN="$(…dex id_token…)" uv run python scripts/seed_medallion_namespaces.py

Empty token = an auth-off local stack. Idempotent: ``adopt_existing`` plus a tolerated 409, so
re-running over a half-seeded estate is the normal way to converge one.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import httpx
import yaml


REPO = pathlib.Path(__file__).resolve().parent.parent

#: 409 from this door means four different things and they share one error code — see the long note in
#: seed_estate.py. Treating it as convergence is right for a seed, whose job is that the object exists.
CONVERGED = (200, 201, 409)


def qualified(project: str, namespace: str) -> str:
    """Project-qualify a namespace exactly as the RUNTIME does, or return it unchanged.

    Byte-identical to ``medallion.workflow._qualified``, and deliberately so: with
    ``medallion.projectsEnabled`` the cascade writes ``<project>-<tier>`` at runtime while the chart
    declares the bare tier names, so a seeder reading the chart provisions namespaces the cascade will
    never ask for and misses every one it will. Measured live 2026-08-26 on tenant ``bind86``: bronze
    existed as ``bind86-bronze``, silver as an unqualified leftover, gold not at all — and the cascade
    ran bronze->silver, landed rows, emitted lineage, then died asking for ``bind86-gold``.

    The two must not drift, so ``tests/unit/test_seed_qualification_matches_runtime.py`` pins them
    against each other rather than trusting this comment.
    """
    if not project or namespace.startswith(f"{project}-"):
        return namespace
    return f"{project}-{namespace}"


def declared_namespaces(values_path: pathlib.Path, project: str = "") -> list[str]:
    """Every top-level namespace the cascade will write into, read from the chart.

    The producer's bronze namespace is the head; each mover names the two it moves between. Ordered
    and de-duplicated so the output reads the way the cascade runs. With ``project`` set, each is
    qualified the way the runtime will ask for it — see :func:`qualified`.
    """
    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    medallion = values.get("medallion") or {}
    seen: dict[str, None] = {}
    head = (medallion.get("producer") or {}).get("bronzeNamespace")
    if head:
        seen[head] = None
    for mover in medallion.get("movers") or []:
        for key in ("fromNamespace", "toNamespace"):
            name = mover.get(key)
            if name:
                seen[name] = None
    return [qualified(project, name) for name in seen]


def create(client: httpx.Client, warehouse: str, namespace: str) -> tuple[int, str]:
    """POST the one door, returning (status, detail). Status 0 = the catalog was unreachable.

    A connection fault is reported as 0 rather than raised so the caller can attempt every namespace
    and report them together — a seed that dies on the first unreachable call tells you less than one
    that tells you none of them landed.
    """
    try:
        response = client.post(f"/v1/warehouses/{warehouse}/namespaces", json={"namespace": namespace, "adopt_existing": True})
    except httpx.HTTPError as exc:
        return 0, str(exc)
    return response.status_code, response.text[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog-url", default=os.environ.get("MEDALLION_CATALOG_URL", "http://localhost:2333"))
    # No default: the previous one ("lance-catalog") was a BUCKET name and could never be a warehouse.
    # Requiring it makes the caller name a real registry warehouse, and the preflight below proves it.
    parser.add_argument("--warehouse", default=os.environ.get("SEED_WAREHOUSE", ""), help="registry warehouse id the namespaces belong to (REQUIRED)")
    parser.add_argument("--token", default=os.environ.get("SEED_CATALOG_TOKEN", ""), help="OIDC bearer; empty = an auth-off stack")
    parser.add_argument("--values", default=str(REPO / "chart/values.yaml"))
    parser.add_argument(
        "--project",
        default=os.environ.get("SEED_PROJECT", ""),
        help=(
            "tenant id; qualifies every tier as <project>-<tier>, exactly as the runtime does when "
            "medallion.projectsEnabled is on. Empty = the single-tenant, unqualified names."
        ),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true", help="print what would be created and exit 0")
    args = parser.parse_args()

    namespaces = declared_namespaces(pathlib.Path(args.values), args.project)
    if not namespaces:
        print("!! the chart declares no medallion namespaces — nothing to seed, which is itself suspicious", file=sys.stderr)
        return 2

    if not args.warehouse:
        print(
            "!! --warehouse is required (or SEED_WAREHOUSE).\n"
            "   It is a REGISTRY WAREHOUSE ID, not the S3 bucket and not the OpenFGA store — all three\n"
            "   are called 'lance-catalog' on a default estate and only the first two exist as that name.\n"
            "   List the real ones with:  GET /v1/warehouses",
            file=sys.stderr,
        )
        return 2

    base = args.catalog_url.rstrip("/")
    print(f"catalog:   {base}")
    print(f"warehouse: {args.warehouse}")
    print(f"bearer:    {'yes' if args.token else 'NO — assuming an auth-off stack'}")
    print(f"project:   {args.project or '(none — single-tenant, unqualified tier names)'}")
    print(f"namespaces ({len(namespaces)}): {', '.join(namespaces)}\n")

    if args.dry_run:
        return 0

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    failed = 0
    with httpx.Client(base_url=base, headers=headers, timeout=args.timeout) as client:
        # PREFLIGHT. A warehouse that is not in the registry turns every create below into a 403/404
        # that reads as a permissions fault. Proving it exists first makes the naming error say so.
        try:
            probe = client.get(f"/v1/warehouses/{args.warehouse}")
        except httpx.HTTPError as exc:
            print(f"!! catalog unreachable: {exc}", file=sys.stderr)
            return 1
        if probe.status_code == 404:
            print(
                f"!! no warehouse {args.warehouse!r} in the registry (HTTP 404).\n"
                f"   If you passed the catalog's own bucket name, note it is RESERVED platform storage and\n"
                f"   can never back a warehouse. List the real ones with GET /v1/warehouses.",
                file=sys.stderr,
            )
            return 1
        if probe.status_code == 403:
            # FGA denies BEFORE it checks existence, so a 403 here does not prove the warehouse exists.
            print(
                f"!! denied reading warehouse {args.warehouse!r} (HTTP 403). The check runs BEFORE the\n"
                f"   existence check, so this does not mean the warehouse is there — it may be absent AND\n"
                f"   unreadable. Confirm with GET /v1/warehouses as an admin before granting anything.",
                file=sys.stderr,
            )
            return 1
        for namespace in namespaces:
            status, detail = create(client, args.warehouse, namespace)
            if status in CONVERGED:
                print(f"  ok      {namespace}  (HTTP {status})")
            else:
                failed += 1
                print(f"  FAILED  {namespace}  (HTTP {status}) {detail}", file=sys.stderr)

    if failed:
        # Non-zero so a Makefile/CI caller sees it. A grant written against a namespace whose create
        # failed is exactly how a ghost is made — seed_estate.py's rule, and it holds here too.
        print(f"\n!! {failed}/{len(namespaces)} namespace(s) not provisioned", file=sys.stderr)
        return 1
    print(f"\n✓ {len(namespaces)} namespace(s) provisioned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
