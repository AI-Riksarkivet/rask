# 0090. The client-direct commit holds each fragment to its data files (LH-211, 2026-10-05)

`/commit` folded a vended writer's `FragmentMetadata` into a version after one existence check per file, and Lance's
commit trusts the rest. Measured on pylance 12.0.0, a false size or row count, out-of-range column indices, a copied
`row_id_meta`, an overlay, a 2.1 or 2.3 file declared 2.2, the same file listed twice (every field id in the table
re-minted), permuted, unknown or tombstoned field ids, a non-bare path, a missing blob sidecar, an external blob naming
any object by absolute URI (which `take_blobs` then served with the reader's credentials) or a missing one, the same file
in two fragments, and a file the table already held (which brought a deleted row back) all committed.

The data file is now the authority (`catalog.services.client_fragments`). The JSON may carry none of what Lance assigns
or what describes an edit, only bare paths, each field id once and each file once, and no file the table at the read
version already holds. Each declared size must be the object's. Each
footer (`LanceFileReader.metadata()`, a HEAD and a ranged GET) must hold `physical_rows` rows at the table's file
version, one column per declared field with `column_indices` 0..n-1, and columns that are, in order, the table fields
the ids name, compared by name and type. On a table with blob columns the fragments are committed detached first and
the last byte of every packed or dedicated sidecar is read; an external blob must name one of the table's registered
bases that is also a configured `LANCE_EXTERNAL_BLOB_BASES` entry, by a relative path to an object that exists and holds
the slice. The detached manifest is deleted with a plain DeleteObject,
because Lance's cleanup leaves it and pyarrow's delete PUTs a `_versions/` marker. The base-id refusal and the declared
version guard stay. Added cost on moto: footers ~1 HEAD + 1 GET per data file, read 8 at a time; a blob table adds
2 PUTs, a LIST and a DELETE per commit plus a GET per fragment and per sidecar, and one batched lookup of every
external blob's object.

Accepted with it: a declared V1/V2 mix is still Lance's own refusal, and an all-V1 table is refused because its footers
cannot be read. What a file's VALUES say (an inline blob's descriptor, a NULL in a non-nullable column) is not checked
(for the register's parking list).
