# 0098. A namespace Overwrite replaces only an empty namespace, and a Skip drop finishes the trailer (LH-037, 2026-10-05)

`CreateNamespaceRequest.mode` Overwrite (`lance_docs/ns_catalog/spec.yaml`: "the existing namespace is dropped and a
new empty namespace with this name is created") is served as drop-then-create with RESTRICT semantics. An empty
namespace needs what `POST /v1/namespace/{id}/drop` needs — `can_delete` on it and no deletion protection — on top of
the create-on-parent rung the router already checked; its tuples, protection and maintenance policy are removed and
the caller is seeded as owner of the new one. Its warehouse binding stays: the new namespace lives where the old one
did. A namespace holding a table or a child namespace answers 409 code 3 (NamespaceNotEmpty) naming them. A cascade

did. So an Overwrite of a BOUND top-level id passes the generic door's warehouse requirement (`LANCE_WAREHOUSES_ENABLED`,
on in the chart) and seeds the replacement's parent edge to that binding's warehouse; only a new, unbound top-level
namespace is still sent to `POST /v1/warehouses/{id}/namespaces`. A namespace holding a table or a child namespace answers 409 code 3 (NamespaceNotEmpty) naming them. A cascade
is not offered on a create door: destroying a subtree, with its trash records and descendant grants, is asked for on
the drop door with `behavior=Cascade`.

`DropNamespaceRequest.mode` Skip over an absent namespace runs the drop's idempotent trailer — protection, policy,
binding, FGA tuples — before answering success, so a retry over a drop that removed the namespace and died before its
trailer leaves nothing a reused id inherits. Not when a live namespace trash record exists: a recoverable drop keeps
the grants and binding for undrop, and the create door refuses the id until the trash is purged. Descendants of an
absent namespace cannot be enumerated, so their grants are not reached here.

The Skip answer is 200 with a `DropNamespaceResponse` body. The mode's prose says 204 while the DropNamespace
operation declares only 200 (spec.yaml), and pylance 12.0.0's `RestNamespace.drop_namespace` refuses a 204 with an
empty body ("Failed to parse response: EOF while parsing a value") and accepts 200 `{}` (measured 2026-10-05). The
authorization gate is unchanged: a caller with no rung on an unknown id still gets the 403 the 2026-09-11 rule keeps
on destructive doors, since `authorize` answers before the endpoint can tell absent from forbidden.
