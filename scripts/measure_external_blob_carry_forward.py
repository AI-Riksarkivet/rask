"""MEASURE: can ingest write External blob descriptors, and do they survive a carry-forward?

Item 1 of open_data_spec.md §8. Two questions, both answered by observation:
  1. Does `blob_array` given URIs produce EXTERNAL descriptors (bytes not copied into the dataset)?
  2. Does a descriptor read from bronze resolve when written into a SECOND dataset (silver)?
"""

import shutil
import tempfile
from pathlib import Path

import lance
import pyarrow as pa
from lance.blob import Blob


tmp = Path(tempfile.mkdtemp(prefix="extblob-"))
payload_dir = tmp / "payloads"
payload_dir.mkdir()

ROWS = 20
uris = []
for i in range(ROWS):
    f = payload_dir / f"page-{i:03d}.bin"
    f.write_bytes(b"X" * 200_000)  # 200 KB each => 4 MB of corpus
    uris.append(f.resolve().as_uri())


def dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


corpus_bytes = dir_bytes(payload_dir)

# ---- A. EXTERNAL: hand blob_array the URIs -------------------------------------------------
ext = str(tmp / "bronze_external.lance")
tbl = pa.table({"id": pa.array(range(ROWS), pa.int64()), "payload": lance.blob_array([Blob.from_uri(u) for u in uris])})
lance.write_dataset(
    tbl,
    ext,
    data_storage_version="2.2",
    enable_stable_row_ids=True,
    initial_bases=[lance.DatasetBasePath(str(payload_dir.resolve()), "payloads")],
    external_blob_mode="reference",
)
ext_bytes = dir_bytes(Path(ext))

# ---- B. MANAGED: hand blob_array the bytes --------------------------------------------------
man = str(tmp / "bronze_managed.lance")
tbl2 = pa.table({"id": pa.array(range(ROWS), pa.int64()), "payload": lance.blob_array([Path(u[7:]).read_bytes() for u in uris])})
lance.write_dataset(tbl2, man, data_storage_version="2.2", enable_stable_row_ids=True)
man_bytes = dir_bytes(Path(man))

print(f"corpus on disk           : {corpus_bytes:>10,} B")
print(f"EXTERNAL dataset on disk : {ext_bytes:>10,} B   ({ext_bytes / corpus_bytes:.1%} of corpus)")
print(f"MANAGED  dataset on disk : {man_bytes:>10,} B   ({man_bytes / corpus_bytes:.1%} of corpus)")

# ---- C. the descriptor shape a scan returns --------------------------------------------------
ds = lance.dataset(ext)
desc = ds.to_table(columns=["payload"])
print("\ndescriptor arrow type:", desc.schema.field("payload").type)
print("first descriptor      :", desc.column("payload")[0].as_py())


# ---- D. does it RESOLVE from the external dataset? --------------------------------------------
def resolves(path: str) -> int:
    d = lance.dataset(path)
    t = d.scanner(columns=["payload"], blob_handling="all_binary").to_table()
    return sum(1 for v in t.column("payload").to_pylist() if v)


print(f"\nEXTERNAL resolves        : {resolves(ext)}/{ROWS}")
print(f"MANAGED  resolves        : {resolves(man)}/{ROWS}")

# ---- E. THE CARRY-FORWARD: descriptor read from bronze, written into silver -------------------
for name, src in (("EXTERNAL", ext), ("MANAGED", man)):
    d = lance.dataset(src)
    carried = d.to_table(columns=["id", "payload"])  # descriptors, no bytes
    vals = carried.column("payload").to_pylist()
    blobs = []
    for v in vals:
        rel = (v or {}).get("blob_uri") or ""
        # THE JOIN: blob_uri is BASE-RELATIVE. Carrying it forward verbatim fails, because a bare
        # relative name matches no registered base on the target. Resolve it against the base first.
        uri = f"{payload_dir.resolve().as_uri()}/{rel}" if rel else ""
        if v and uri and v.get("kind") == 3:
            # size==0 means "the whole object" — passing 0 through would request a ZERO-LENGTH slice.
            size, pos = v.get("size") or 0, v.get("position") or 0
            blobs.append(Blob(uri=uri, position=pos, size=size) if size else Blob(uri=uri))
        elif v and uri:
            blobs.append(Blob(uri=uri))
        else:
            blobs.append(None)
    silver = str(tmp / f"silver_{name.lower()}.lance")
    try:
        st = pa.table({"id": carried.column("id"), "payload": lance.blob_array(blobs)})
        lance.write_dataset(
            st,
            silver,
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            initial_bases=[lance.DatasetBasePath(str(payload_dir.resolve()), "payloads")],
            external_blob_mode="reference",
        )
        print(f"{name:9s} carried -> silver resolves: {resolves(silver)}/{ROWS}   silver on disk {dir_bytes(Path(silver)):,} B")
    except Exception as e:
        print(f"{name:9s} carried -> silver FAILED: {type(e).__name__}: {str(e)[:160]}")

shutil.rmtree(tmp)
