#!/usr/bin/env python3
"""MAPI namespace operations: list, create, update, export, delete.

Requires HCP_ENDPOINT, HCP_USERNAME, HCP_PASSWORD, HCP_TENANT env vars.
"""

from __future__ import annotations

import asyncio
import json

from rahcp_client import HCPClient

TENANT = "dev-ai"


async def main() -> None:
    async with HCPClient.from_env() as client:
        # --8<-- [start:namespace-crud]
        # List existing namespaces
        namespaces = await client.mapi.list_namespaces(TENANT, verbose=True)
        print(f"Namespaces in '{TENANT}':")
        for ns in namespaces:
            print(f"  - {ns['name']}: {ns.get('description', '(no description)')}")

        # Create a new namespace
        await client.mapi.create_namespace(
            TENANT,
            {
                "name": "example-ns",
                "description": "Created by example script",
                "hardQuota": "50 GB",
                "softQuota": 80,
            },
        )
        print("\nCreated 'example-ns' (50 GB quota, 80% soft)")

        # Get namespace details
        ns = await client.mapi.get_namespace(TENANT, "example-ns", verbose=True)
        print(f"\nDetails: {json.dumps(ns, indent=2)}")

        # Update namespace
        await client.mapi.update_namespace(
            TENANT,
            "example-ns",
            {
                "description": "Updated by example script",
                "hardQuota": "100 GB",
            },
        )
        print("\nUpdated 'example-ns' → 100 GB quota")
        # --8<-- [end:namespace-crud]

        # --8<-- [start:namespace-export]
        # Export namespace as a reusable template
        template = await client.mapi.export_namespace(TENANT, "example-ns")
        print(f"\nExported template:\n{json.dumps(template, indent=2)}")

        # Export multiple namespaces as a bundle
        bundle = await client.mapi.export_namespaces(TENANT, ["example-ns"])
        print(f"\nBundle: {json.dumps(bundle, indent=2)}")
        # --8<-- [end:namespace-export]

        # Delete namespace
        await client.mapi.delete_namespace(TENANT, "example-ns")
        print("\nDeleted 'example-ns'")


if __name__ == "__main__":
    asyncio.run(main())
