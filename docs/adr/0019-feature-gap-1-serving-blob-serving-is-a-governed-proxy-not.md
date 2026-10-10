# 0019. FEATURE-GAP §1 (serving) — blob serving is a governed proxy, not presigned URLs

**Decision.** Credential-less consumers (browser, notebook) fetch blob bytes back through the catalog:
`GET /management/v1/table/{id}/blobs?column=&row=[&version=]` streams the bytes with RFC 9110 Range support — a
`Range: bytes=…` request reads only the window from storage via the lazy `BlobFile` (206 +
`Content-Range`; 416 when unsatisfiable) — governed at reader-tier `can_read_data` like `/query`.
Deliberately a governed proxy, **not** presigned URLs: a signed URL bypasses ReBAC for its TTL.
Blob modes managed/inline/packed/dedicated (bytes copied in) always work; **external-pointer**
(`Blob.from_uri` outside the dataset root) is gated behind `vending.allowExternalBlobs` (default off —
an external object's lifecycle is outside Lance's version-aware GC) and rejected with a clean 400 when off.

**Rationale.** The catalog vends storage access; handing out a URL that answers without an FGA check for
its lifetime would punch a ReBAC hole exactly at the highest-value bytes (media blobs). Range support
keeps the proxy viable for large blobs (a viewer reads a window, not the object).
