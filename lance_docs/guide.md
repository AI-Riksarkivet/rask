Directory structure:
└── guide/
    ├── arrays.md
    ├── blob.md
    ├── data_evolution.md
    ├── data_types.md
    ├── distributed_indexing.md
    ├── distributed_write.md
    ├── json.md
    ├── migration.md
    ├── object_store.md
    ├── observability.md
    ├── performance.md
    ├── read_and_write.md
    ├── tags_and_branches.md
    ├── tokenizer.md
    └── .pages


Files Content:


================================================
FILE: docs/src/guide/arrays.md
================================================
# Extension Arrays

Lance provides extensions for Arrow arrays and Pandas Series to represent data
types for machine learning applications.

## BFloat16

[BFloat16](https://cloud.google.com/blog/products/ai-machine-learning/bfloat16-the-secret-to-high-performance-on-cloud-tpus)
is a 16-bit floating point number that is designed for machine learning use cases.
Intuitively, it only has 2-3 digits of precision, but it has the same range as
a 32-bit float: ~1e-38 to ~1e38. By comparison, a 16-bit float has a range of
~5.96e-8 to 65504.

Lance provides an Arrow extension array (`lance.arrow.BFloat16Array`)
and a Pandas extension array (`lance._arrow.PandasBFloat16Type`) for BFloat16.
These are compatible with the [ml_dtypes](https://github.com/jax-ml/ml_dtypes)
bfloat16 NumPy extension array.

If you are using Pandas, you can use the `lance.bfloat16` dtype string to create
the array:

```python
import lance.arrow

pd.Series([1.1, 2.1, 3.4], dtype="lance.bfloat16")
# 0    1.1015625
# 1      2.09375
# 2      3.40625
# dtype: lance.bfloat16
```

To create an Arrow array, use the `lance.arrow.bfloat16_array` function:

```python
from lance.arrow import bfloat16_array

bfloat16_array([1.1, 2.1, 3.4])
# <lance.arrow.BFloat16Array object at 0x000000016feb94e0>
# [
#   1.1015625,
#   2.09375,
#   3.40625
# ]
```

Finally, if you have a pre-existing NumPy array, you can convert it into either:

```python
import numpy as np
from ml_dtypes import bfloat16
from lance.arrow import PandasBFloat16Array, BFloat16Array

np_array = np.array([1.1, 2.1, 3.4], dtype=bfloat16)
PandasBFloat16Array.from_numpy(np_array)
# <PandasBFloat16Array>
# [1.1015625, 2.09375, 3.40625]
# Length: 3, dtype: lance.bfloat16
BFloat16Array.from_numpy(np_array)
# <lance.arrow.BFloat16Array object at 0x...>
# [
#   1.1015625,
#   2.09375,
#   3.40625
# ]
```

When reading, these can be converted back to to the NumPy bfloat16 dtype using
each array class's `to_numpy` method.

## ImageURI

`lance.arrow.ImageURIArray` is an array that stores the URI location of images
in some other storage system. For example, `file:///path/to/image.png` for a local
filesystem or `s3://bucket/path/image.jpeg` for an image on AWS S3. Use this
array type when you want to lazily load images from an existing storage medium.

It can be created by calling `lance.arrow.ImageURIArray.from_uris`
with a list of URIs represented by either `pyarrow.StringArray` or an
iterable that yields strings. Note that the URIs are not strongly validated and images
are not read into memory automatically.

```python
from lance.arrow import ImageURIArray

ImageURIArray.from_uris([
   "/tmp/image1.jpg",
   "file:///tmp/image2.jpg",
   "s3://example/image3.jpg"
])
# <lance.arrow.ImageURIArray object at 0x...>
# ['/tmp/image1.jpg', 'file:///tmp/image2.jpg', 's3://example/image3.jpg']
```

`lance.arrow.ImageURIArray.read_uris` will read images into memory and return
them as a new `lance.arrow.EncodedImageArray` object.

```python
from lance.arrow import ImageURIArray

relative_path = "images/1.png"
uris = [os.path.join(os.path.dirname(__file__), relative_path)]
ImageURIArray.from_uris(uris).read_uris()
# <lance.arrow.EncodedImageArray object at 0x...>
# [b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00...']
```

## EncodedImage

`lance.arrow.EncodedImageArray` is an array that stores jpeg and png images in
their encoded and compressed representation as they would appear written on disk.
Use this array when you want to manipulate images in their compressed format such as
when you're reading them from disk or embedding them into HTML.

It can be created by calling `lance.arrow.ImageURIArray.read_uris` on an existing
`lance.arrow.ImageURIArray`. This will read the referenced images into memory.
It can also be created by calling `lance.arrow.ImageArray.from_array` and passing
it an array of encoded images already read into `pyarrow.BinaryArray` or by
calling `lance.arrow.ImageTensorArray.to_encoded`.

A `lance.arrow.EncodedImageArray.to_tensor` method is provided to decode
encoded images and return them as `lance.arrow.FixedShapeImageTensorArray`, from
which they can be converted to numpy arrays.
For decoding images, it will first attempt to use a decoder provided via the optional
function parameter. If decoder is not provided it will attempt to use
[Pillow](https://pillow.readthedocs.io/en/stable/). If neither Pillow nor a custom
decoder is available an exception will be raised.

```python
from lance.arrow import ImageURIArray

uris = [os.path.join(os.path.dirname(__file__), "images/1.png")]
encoded_images = ImageURIArray.from_uris(uris).read_uris()
print(encoded_images.to_tensor())

def pillow_decoder(images):
    import io
    import numpy as np
    from PIL import Image

    return np.stack(
        np.asarray(Image.open(io.BytesIO(img.as_py()))) for img in images.storage
    )

print(encoded_images.to_tensor(pillow_decoder))
# <lance.arrow.FixedShapeImageTensorArray object at 0x...>
# [[42, 42, 42, 255]]
# <lance.arrow.FixedShapeImageTensorArray object at 0x...>
# [[42, 42, 42, 255]]
```

## FixedShapeImageTensor

`lance.arrow.FixedShapeImageTensorArray` is an array that stores images as
tensors where each individual pixel is represented as a numeric value. Typically images
are stored as 3 dimensional tensors shaped (height, width, channels). In color images
each pixel is represented by three values (channels) as per
[RGB color model](https://en.wikipedia.org/wiki/RGB_color_model).
Images from this array can be read out as numpy arrays individually or stacked together
into a single 4 dimensional numpy array shaped (batch_size, height, width, channels).

It can be created by calling `lance.arrow.EncodedImageArray.to_tensor` on a
previously existing `lance.arrow.EncodedImageArray`. This will decode encoded
images and return them as a `lance.arrow.FixedShapeImageTensorArray`. It can also be
created by calling `lance.arrow.ImageArray.from_array` and passing in a
`pyarrow.FixedShapeTensorArray`.

It can be encoded into to `lance.arrow.EncodedImageArray` by calling
`lance.arrow.FixedShapeImageTensorArray.to_encoded` and passing custom encoder
If encoder is not provided it will attempt to use
[Pillow](https://pillow.readthedocs.io/en/stable/). The default encoder will encode
to PNG. If neither Pillow nor a custom encoder is available it will raise an exception.

```python
from lance.arrow import ImageURIArray

uris = [image_uri]
tensor_images = ImageURIArray.from_uris(uris).read_uris().to_tensor()
tensor_images.to_encoded()
# <lance.arrow.EncodedImageArray object at 0x...>
# [...
# b'\x89PNG\r\n\x1a...'
```


================================================
FILE: docs/src/guide/blob.md
================================================
# Blob Columns

Lance can store large binary objects (images, videos, audio, model artifacts) in blob columns, where they are treated like any other column payload in the dataset.
Blob columns support both planned full-payload reads and lazy file-like access.

!!! tip "Choosing between `read_blobs` and `take_blobs`"
    - For data loaders and batch processing that need complete byte payloads, use `read_blobs`.
    - Use `take_blobs` when you need a `BlobFile` handle for streaming, seeking, or partial reads.


![Blob v2 overview](../images/blob-v2-overview-light.png#only-light)
![Blob v2 overview](../images/blob-v2-overview-dark.png#only-dark)

If you're unsure about whether you need a blob column in the first place (and why it's useful), read the "[when to use blob column vs. inline binary](#when-to-use-a-blob-column-vs-inline-binary)" section below.

## Quick Start: Blob v2

This page focuses on blob workflows in Python and uses Lance file format terminology.

- `data_storage_version` means the Lance **file format version** of a dataset.
- A dataset has a default file format version. Append and compaction can select
  another exact `2.x` version without rewriting existing files or changing that default.
- File format `0.1` cannot be mixed with the `2.x` family.

```python
import lance
import pyarrow as pa
from lance import blob_array, blob_field

schema = pa.schema([
    pa.field("id", pa.int64()),
    blob_field("blob"),
])

table = pa.table(
    {
        "id": [1],
        "blob": blob_array([b"hello blob v2"]),
    },
    schema=schema,
)

ds = lance.write_dataset(table, "./blobs_v22.lance", data_storage_version="2.2")

_row_address, payload = ds.read_blobs("blob", indices=[0])[0]
assert payload == b"hello blob v2"
```

## Version Compatibility

Blob support is tied to the dataset's file format version. Earlier file format versions
(`< 2.2`) stored blobs using the `lance-encoding:blob` metadata field, while Blob
v2 introduces a new storage layout that requires file format `>= 2.2`.

The target file version selects the physical Blob encoding. For `>= 2.2`,
dataset writers also accept legacy blob-marked binary inputs and convert them
to Blob v2 before encoding. Older files keep their original encoding.

| Dataset `data_storage_version` | Legacy blob metadata (`lance-encoding:blob`) | Blob v2 (`lance.blob.v2`) |
|---|---|---|
| `0.1`, `2.0`, `2.1` | Supported for write/read | Not supported |
| `2.2+` | Accepted as input; written as Blob v2 | Supported for write/read (recommended) |

### Mixing legacy Blob and Blob v2 files

Append with `data_storage_version="2.2"` to start writing Blob v2 into an
existing `2.0` or `2.1` dataset. Both legacy blob-marked binary inputs and
`blob_field` / `blob_array` inputs are accepted. No separate upgrade operation
or historical-payload rewrite is required.

When the first Blob v2 file for a legacy column is committed, the snapshot uses
the Blob v2 logical schema for that column. Descriptor scans return the v2
`kind, position, size, blob_id, blob_uri` struct across all fragments, including
selections containing only old files. Old payloads appear as Inline descriptors
pointing into their original data files. `take_blobs`, `read_blobs`, and
`blob_handling="all_binary"` continue to expose the same payloads, nulls, and
empty values. Historical snapshots retain their previous schema.

Descriptor consumers that access fields by position or compare exact Arrow
schemas must account for the extended structure. Interpret `position` together
with `kind` and the object reference. See [the schema-transition discussion](https://github.com/lance-format/lance/issues/9458).

The dataset's default file version remains unchanged. Select `2.2` or newer
explicitly when writing Blob v2 structs or compacting the mixed column into
Blob v2 files. Compaction rewrites the selected fragments; it is not required
to enable new Blob v2 writes.

## Blob v2: Write Patterns

Use `blob_field` and `blob_array` to build blob v2 columns.

### Logical Arrow schema

A blob v2 field is tagged with `ARROW:extension:name = "lance.blob.v2"`. Writers
accept these logical struct shapes:

| Shape | Children | Use |
|---|---|---|
| Minimal | `data: LargeBinary?`, `uri: Utf8?` | Inline bytes or a complete external object |
| Complete | Minimal fields plus `position: UInt64?`, `size: UInt64?` | An optional byte range within an external object |

Every non-null row must set exactly one of `data` and `uri`. For the complete
shape, `position` and `size` must either both be set or both be null, a range
requires `uri`, and an explicit range must have `size > 0`. Use inline `b""` for
an empty blob; a URI without range fields still represents the complete external
object, including an empty object. Python's `blob_field` and `BlobType` use the
complete shape. Lance preserves an accepted logical shape, including child
fields, nullability, and metadata, across create, append, and merge-insert
writes; descriptor scans still return the compact stored descriptor shape.

```python
import lance
import pyarrow as pa
from lance import Blob, blob_array, blob_field

schema = pa.schema([
    pa.field("id", pa.int64()),
    blob_field("blob", nullable=True),
])

# A single column can mix:
# - inline bytes
# - external URI
# - external URI slice (position + size)
# - null
rows = pa.table(
    {
        "id": [1, 2, 3, 4],
        "blob": blob_array([
            b"inline-bytes",
            "s3://bucket/path/video.mp4",
            Blob.from_uri("s3://bucket/archive.tar", position=4096, size=8192),
            None,
        ]),
    },
    schema=schema,
)

ds = lance.write_dataset(
    rows,
    "./blobs_v22.lance",
    data_storage_version="2.2",
)
```

Note:

- By default, external blob URIs must map to a registered non-dataset-root base path.
- If you need to reference external objects outside those bases, set
  `allow_external_blob_outside_bases=True` when writing.
- Blob v2 storage layout thresholds can be configured per column with
  `blob_field(..., inline_size_threshold=..., dedicated_size_threshold=...)`.
  The inline threshold controls when values move from the data file to packed
  `.blob` sidecar storage. The dedicated threshold controls when values move
  from packed sidecar storage to a dedicated `.blob` file. The dedicated
  threshold is checked first. For existing columns, these thresholds are stored
  in the dataset schema; appends that explicitly provide different threshold
  metadata for the same column are rejected.
- `blob_pack_file_size_threshold` is a write option for rolling packed `.blob`
  sidecar files. It does not control inline-vs-packed placement.
- Blob v2 fields can be nested inside structs and variable-length lists. Blob-aware
  scans preserve the surrounding nested layout; use `blob_handling="all_binary"`
  to materialize nested blob payloads as bytes.

### Example: packed external blobs (single container file)

```python
import io
import tarfile
from pathlib import Path
import lance
import pyarrow as pa
from lance import Blob, blob_array, blob_field

# Build a tar file with three payloads
payloads = {
    "a.bin": b"alpha",
    "b.bin": b"bravo",
    "c.bin": b"charlie",
}

with tarfile.open("container.tar", "w") as tf:
    for name, data in payloads.items():
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

# Capture offset/size for each member
blob_values = []
with tarfile.open("container.tar", "r") as tf:
    container_uri = Path("container.tar").resolve().as_uri()
    for name in payloads:
        m = tf.getmember(name)
        blob_values.append(Blob.from_uri(container_uri, position=m.offset_data, size=m.size))

schema = pa.schema([
    pa.field("name", pa.utf8()),
    blob_field("blob"),
])

rows = pa.table(
    {
        "name": list(payloads.keys()),
        "blob": blob_array(blob_values),
    },
    schema=schema,
)

ds = lance.write_dataset(
    rows,
    "./packed_blobs_v22.lance",
    data_storage_version="2.2",
    allow_external_blob_outside_bases=True,
)
```

## Blob v2: Read Patterns

Choose the read API based on the payload shape you want:

| API | Returns | Use When |
|---|---|---|
| `read_blobs` | `List[Tuple[int, Optional[bytes]]]` | You need complete blob payloads in memory, such as training loaders or batch preprocessing. |
| `read_blob_ranges` | `List[Tuple[int, int, Optional[bytes]]]` | You need selected byte ranges from multiple rows without materializing complete blobs. |
| `take_blobs` | `List[Optional[BlobFile]]` | You need file-like objects for streaming, seeking, or partial reads. |
| `scanner(..., blob_handling="all_binary")` | Arrow binary columns | You want blob columns in a scan result or `pyarrow.Table`. |

Do not wrap `take_blobs` in your own thread pool just to call `read()` or
`readall()` on every blob. Use `read_blobs` instead; it plans and executes
batched blob reads through Lance's scheduler.

Exactly one selector must be provided to `read_blobs` or `take_blobs`: `ids`,
`indices`, or `addresses`. `read_blob_ranges` accepts the same selector kinds
through its required `selector` argument.

| Selector | Typical Use | Stability |
|---|---|---|
| `indices` | Positional reads within one dataset snapshot | Stable within that snapshot |
| `ids` | Logical row-id based reads | Stable logical identity (when row ids are available) |
| `addresses` | Low-level physical reads and debugging | Unstable physical location |

### Read complete payloads by row indices

```python
import lance

ds = lance.dataset("./blobs_v22.lance")
rows = ds.read_blobs("blob", indices=[0, 1])
payloads = [payload for _row_address, payload in rows]
```

### Read complete payloads by row ids

```python
import lance

ds = lance.dataset("./blobs_v22.lance")
row_ids = ds.to_table(columns=[], with_row_id=True).column("_rowid").to_pylist()

rows = ds.read_blobs("blob", ids=row_ids[:2])
```

### Read complete payloads by row addresses

```python
import lance

ds = lance.dataset("./blobs_v22.lance")
row_addrs = ds.to_table(columns=[], with_row_address=True).column("_rowaddr").to_pylist()

rows = ds.read_blobs("blob", addresses=row_addrs[:2])
```

Blob selection APIs preserve logical result cardinality. `read_blobs()` and
`take_blobs()` return one element per selected row, and `read_blob_ranges()`
returns one element per request. A null blob is returned as `None`; a valid
empty blob remains a non-null empty payload or zero-length `BlobFile`.

### Read row-specific byte ranges

Use `read_blob_ranges` to read multiple blob-local ranges with one planned API
call. Each request is a `(row, offset, length)` tuple, and `selector` determines
whether every `row` is interpreted as a row ID, row address, or dataset index.

```python
import lance

ds = lance.dataset("./blobs_v22.lance")
results = ds.read_blob_ranges(
    "blob",
    requests=[
        (7, 0, 1024),
        (7, 4096, 1024),
        (12, 0, 0),
    ],
    selector="indices",
)

for request_index, row_address, data in results:
    if data is None:
        # The selected blob is null.
        continue
    print(request_index, row_address, len(data))
```

Each result contains the zero-based `request_index`, the resolved physical row
address, and the requested bytes. `request_index` identifies the original
request when the same row appears more than once.

A request on a null blob returns `None`, including when its range is empty. An
empty range on a non-null blob returns `b""` without payload I/O. For every
request, `offset + length` must fit in an unsigned 64-bit integer. A range on a
non-null blob must not extend beyond its logical size; blob-local bounds are not
evaluated for null blobs because they have no logical payload length.

### Read blob columns as Arrow binary

```python
import lance

ds = lance.dataset("./blobs_v22.lance")
table = ds.scanner(columns=["blob"], blob_handling="all_binary").to_table()
payloads = table.column("blob").to_pylist()
```

### Open file-like blob handles lazily

Sequential reads are buffered, 512 KiB by default. Pass `buffer_size=0`
for unbuffered reads. `read_range` and `read_ranges` do not use this
buffer.

```python
import lance

ds = lance.dataset("./blobs_v22.lance")
blobs = ds.take_blobs("blob", indices=[0, 1])

blob = blobs[0]
if blob is not None:
    with blob as f:
        header = f.read(1024)
```

### Example: decode video frames lazily

```python
import av
import lance

ds = lance.dataset("./videos_v22.lance")
blob = ds.take_blobs("video", indices=[0])[0]
if blob is None:
    raise ValueError("video blob is null")

start_ms, end_ms = 500, 1000

with av.open(blob) as container:
    stream = container.streams.video[0]
    stream.codec_context.skip_frame = "NONKEY"

    start = (start_ms / 1000) / stream.time_base
    end = (end_ms / 1000) / stream.time_base
    container.seek(int(start), stream=stream)

    for frame in container.decode(stream):
        if frame.time is not None and frame.time > end_ms / 1000:
            break
        # process frame
        pass
```

## Legacy Compatibility (`data_storage_version` <= `2.1`)

If you need to keep writing legacy blob columns, use file format `0.1`, `2.0`, or `2.1`
and mark `LargeBinary` fields with a metadata kwarg `"lance-encoding:blob": true`.

```python
import lance
import pyarrow as pa

schema = pa.schema([
    pa.field("id", pa.int64()),
    pa.field(
        "video",
        pa.large_binary(),
        metadata={"lance-encoding:blob": "true"},
    ),
])

table = pa.table(
    {
        "id": [1, 2],
        "video": [b"foo", b"bar"],
    },
    schema=schema,
)

ds = lance.write_dataset(
    table,
    "./legacy_blob_dataset",
    data_storage_version="2.1",
)
```

With `data_storage_version >= 2.2`, dataset writers convert this input to Blob v2.
For new datasets, prefer `blob_field` / `blob_array` with file format 2.2.

## Rewrite to a New Blob v2 Dataset

To replace all historical legacy Blob encodings at once, you can rewrite them
into a new dataset with `data_storage_version="2.2"`. This is optional for
`2.0` / `2.1` tables, which can instead append Blob v2 files as described above.
File format `0.1` tables still require a rewrite to move into the `2.x` family.

```python
import lance
import pyarrow as pa
from lance import blob_array, blob_field

legacy = lance.dataset("./legacy_blob_dataset")
raw = legacy.scanner(columns=["id", "video"], blob_handling="all_binary").to_table()

new_schema = pa.schema([
    pa.field("id", pa.int64()),
    blob_field("video"),
])

rewritten = pa.table(
    {
        "id": raw.column("id"),
        "video": blob_array(raw.column("video").to_pylist()),
    },
    schema=new_schema,
)

lance.write_dataset(
    rewritten,
    "./blob_v22_dataset",
    data_storage_version="2.2",
)
```

!!! warning
    - The example above materializes binary payloads in memory (`blob_handling="all_binary"` and `to_pylist()`).
    - For large datasets, prefer chunked/batched rewrite pipelines.

## When to Use a Blob Column vs. Inline Binary

Not every binary column needs to be a blob column. Plain Arrow `binary`/`large_binary` stores bytes *inline*, interleaved with your other columns, which is simplest and fastest for really small blobs (e.g., thumbnail images). Using a blob column to store the binary payload makes sense when either of these holds:

- **You need partial or streaming reads.** Inline binary is always read in full; there is no way to fetch a byte range without materializing the entire value. Blob columns expose `read_blob_ranges` for planned row-specific range reads and `take_blobs` → `BlobFile` handles for caller-driven seeks, so you pay only for the bytes you touch.
- **Your values are large (roughly 1 MB or more on average).** Operations that rewrite entire rows, such as compaction or some updates, must copy the large inline payloads forward into the new version — even when those bytes never changed. The bigger the payload, the more bytes you rewrite per logical change (write amplification). A blob column keeps large payloads in separate `.blob` files that are referenced rather than re-copied, so these operations don't rewrite the heavy bytes.

!!! tip
    As a rule of thumb, if average payload size is below a few tens of KB and you only ever read whole values, plain inline binary is fine. Above ~1 MB, or any time you want file-like access, prefer a blob column. Blob v2 also tunes this automatically: by default it keeps payloads up to 64 KiB inline, packs payloads over 64 KiB and up to 4 MiB into shared `.blob` sidecars, and gives payloads over 4 MiB their own dedicated `.blob` file. These thresholds are configurable per column.

## Troubleshooting

This section contains commonly noticed issues or errors, and explains how to address them.

### Blob v2 requires file version >= 2.2
**Cause**: You are writing blob v2 values into a dataset/file format below `2.2`.
**Fix**: Write to a dataset created with `data_storage_version="2.2"` (or newer).

### Legacy blob columns ... are not supported for file version >= 2.2
**Cause**: A lower-level file writer received legacy blob metadata instead of prepared Blob v2 input.
**Fix**: Use the dataset writer, which adapts legacy byte inputs, or prepare Blob v2 columns with the file-level Blob APIs.


### Exactly one of ids, indices, or addresses must be specified
**Cause**: `read_blobs` or `take_blobs` received none or multiple selectors.
**Fix**: Provide exactly one of `ids`, `indices`, or `addresses`.


================================================
FILE: docs/src/guide/data_evolution.md
================================================
# Data Evolution

Lance supports traditional schema evolution: adding, removing, and altering columns in a
dataset. Most of these operations can be performed *without* rewriting the
data files in the dataset, making them very efficient operations.
In addition, Lance supports **data evolution**, which allows you to also backfill existing rows
with the new column data without rewriting the data files in the dataset,
making it highly suitable for use cases like ML feature engineering.

In general, schema changes will conflict with most other concurrent write
operations. For example, if you change the schema of the dataset while someone
else is appending data to it, either your schema change or the append will fail,
depending on the order of the operations. Thus, it's recommended to perform
schema changes when no other writes are happening.

## Adding new columns

### Schema only

A common use case we've seen in production is to add a new column to a dataset without
populating it. This is useful to later run a large distributed job to populate the column
lazily. To do this, you can use the `lance.LanceDataset.add_columns` method to
add columns with `pyarrow.Field` or `pyarrow.Schema`.

```python
table = pa.table({"id": pa.array([1, 2, 3])})
dataset = lance.write_dataset(table, "null_columns")

# With pyarrow Field
dataset.add_columns(pa.field("embedding", pa.list_(pa.float32(), 128)))
assert dataset.schema == pa.schema([
    ("id", pa.int64()),
    ("embedding", pa.list_(pa.float32(), 128)),
])

# With pyarrow Schema
dataset.add_columns(pa.schema([
    ("label", pa.string()),
    ("score", pa.float32()),
]))
assert dataset.schema == pa.schema([
    ("id", pa.int64()),
    ("embedding", pa.list_(pa.float32(), 128)),
    ("label", pa.string()),
    ("score", pa.float32()),
])
```

This operation is very fast, as it only updates the metadata of the dataset.

For Lance file format `<= 2.1`, adding sub-columns under an existing `struct` is not supported.
Starting with Lance file format `2.2`, schema-only add can also extend nested `struct` fields
(including `struct` fields nested inside list types), for example by adding
`people.item.location` under `list<struct<...>>`.

### With data backfill

New columns can be added and populated within a single operation using the
`lance.LanceDataset.add_columns` method. There are two ways to specify
how to populate the new columns: first, by providing a SQL expression for each
new column, or second, by providing a function to generate the new column data.

SQL expressions can either be independent expressions or reference existing
columns. SQL literal values can be used to set a single value for all
existing rows.

```python
table = pa.table({"name": pa.array(["Alice", "Bob", "Carla"])})
dataset = lance.write_dataset(table, "names")
dataset.add_columns({
    "hash": "sha256(name)",
    "status": "'active'",
})
print(dataset.to_table().to_pandas())
#     name                                               hash  status
# 0  Alice  b';\xc5\x10b\x97<E\x8dZo-\x8dd\xa0#$cT\xad~\x0...  active
# 1    Bob  b'\xcd\x9f\xb1\xe1H\xcc\xd8D.Z\xa7I\x04\xccs\x...  active
# 2  Carla  b'\xad\x8d\x83\xff\xd8+Z\x8e\xd4)\xe8Y+\\\xb3\...  active
```

You can also provide a Python function to generate the new column data. This can
be used, for example, to compute a new embedding column. This function should
take a PyArrow RecordBatch and return either a PyArrow RecordBatch or a Pandas
DataFrame. The function will be called once for each batch in the dataset.

If the function is expensive to compute and can fail, it is recommended to set
a checkpoint file in the UDF. This checkpoint file saves the state of the UDF
after each invocation, so that if the UDF fails, it can be restarted from the
last checkpoint. Note that this file can get quite large, since it needs to store
unsaved results for up to an entire data file.

```python
import lance
import pyarrow as pa
import numpy as np

table = pa.table({"id": pa.array([1, 2, 3])})
dataset = lance.write_dataset(table, "ids")

@lance.batch_udf(checkpoint_file="embedding_checkpoint.sqlite")
def add_random_vector(batch):
    embeddings = np.random.rand(batch.num_rows, 128).astype("float32")
    return pa.RecordBatch.from_arrays(
        [pa.FixedSizeListArray.from_arrays(embeddings.flatten(), 128)],
        names=["embedding"]
    )
dataset.add_columns(add_random_vector)
```

### Using merge

If you have pre-computed one or more new columns, you can add them to an existing
dataset using the `lance.LanceDataset.merge` method. This allows filling in
additional columns without having to rewrite the whole dataset.

To use the `merge` method, provide a new dataset that includes the columns you
want to add, and a column name to use for joining the new data to the existing
dataset.

For example, imagine we have a dataset of embeddings and ids:

```python
table = pa.table({
   "id": pa.array([1, 2, 3]),
   "embedding": pa.array([np.array([1, 2, 3]), np.array([4, 5, 6]),
                          np.array([7, 8, 9])])
})
dataset = lance.write_dataset(table, "embeddings", mode="overwrite")
```

Now if we want to add a column of labels we have generated, we can do so by merging a new table:

```python
new_data = pa.table({
   "id": pa.array([1, 2, 3]),
   "label": pa.array(["horse", "rabbit", "cat"])
})
dataset.merge(new_data, "id")
print(dataset.to_table().to_pandas())
#    id  embedding   label
# 0   1  [1, 2, 3]   horse
# 1   2  [4, 5, 6]  rabbit
# 2   3  [7, 8, 9]     cat
```

## Dropping columns

Finally, you can drop columns from a dataset using the `lance.LanceDataset.drop_columns`
method. This is a metadata-only operation and does not delete the data on disk. This makes
it very quick.

```python
table = pa.table({"id": pa.array([1, 2, 3]),
                 "name": pa.array(["Alice", "Bob", "Carla"])})
dataset = lance.write_dataset(table, "names", mode="overwrite")
dataset.drop_columns(["name"])
print(dataset.schema)
# id: int64
```

Starting with Lance file format `2.2`, nested sub-column removal is supported for
nested types (for example `people.item.city` on `list<struct<...>>`), instead of
being limited to `struct` only.

To actually remove the data from disk, the files must be rewritten to remove the
columns and then the old files must be deleted. This can be done using
`lance.dataset.DatasetOptimizer.compact_files()` followed by
`lance.LanceDataset.cleanup_old_versions()`.

!!! warning

    `drop_columns` is metadata-only and remains reversible as long as old versions are retained.
    After `compact_files()` rewrites data files and `cleanup_old_versions()` removes old manifests/files,
    removed data may become permanently unrecoverable.

    For production workflows, use a rollback window:
    - create a tag (or snapshot/backup) before nested column drops
    - delay cleanup until the rollback window has passed
    - only run aggressive cleanup after rollback validation

## Renaming columns

Columns can be renamed using the `lance.LanceDataset.alter_columns` method.

```python
table = pa.table({"id": pa.array([1, 2, 3])})
dataset = lance.write_dataset(table, "ids")
dataset.alter_columns({"path": "id", "name": "new_id"})
print(dataset.to_table().to_pandas())
#    new_id
# 0       1
# 1       2
# 2       3
```

This works for nested columns as well. To address a nested column, use a dot
(`.`) to separate the levels of nesting. For example:

```python
data = [
  {"meta": {"id": 1, "name": "Alice"}},
  {"meta": {"id": 2, "name": "Bob"}},
]
schema = pa.schema([
    ("meta", pa.struct([
        ("id", pa.int32()),
        ("name", pa.string()),
    ]))
])
dataset = lance.write_dataset(data, "nested_rename")
dataset.alter_columns({"path": "meta.id", "name": "new_id"})
print(dataset.to_table().to_pandas())
#                                  meta
# 0  {'new_id': 1, 'name': 'Alice'}
# 1    {'new_id': 2, 'name': 'Bob'}
```

## Casting column data types

In addition to changing column names, you can also change the data type of a
column using the `lance.LanceDataset.alter_columns` method. This
requires rewriting that column to new data files, but does not require rewriting
the other columns.

!!! note

    If the column has an index, the index will be dropped if the column type is
    changed.

This method can be used to change the vector type of a column. For example, we
can change a float32 embedding column into a float16 column to save disk space
at the cost of lower precision:

```python
table = pa.table({
   "id": pa.array([1, 2, 3]),
   "embedding": pa.FixedShapeTensorArray.from_numpy_ndarray(
       np.random.rand(3, 128).astype("float32"))
})
dataset = lance.write_dataset(table, "embeddings")
dataset.alter_columns({"path": "embedding",
                       "data_type": pa.list_(pa.float16(), 128)})
print(dataset.schema)
# id: int64
# embedding: fixed_size_list<item: halffloat>[128]
#   child 0, item: halffloat
```


================================================
FILE: docs/src/guide/data_types.md
================================================
# Data Types

Lance uses [Apache Arrow](https://arrow.apache.org/) as its in-memory data format. This guide covers the supported data types with a focus on array types, which are essential for vector embeddings and machine learning applications.

## Arrow Type System

Lance supports the full Apache Arrow type system. When writing data through Python (PyArrow) or Rust (arrow-rs), the Arrow types are automatically mapped to Lance's internal representation.

### Primitive Types

| Arrow Type | Description | Example Use Case |
|------------|-------------|------------------|
| `Boolean` | True/false values | Flags, filters |
| `Int8`, `Int16`, `Int32`, `Int64` | Signed integers | IDs, counts |
| `UInt8`, `UInt16`, `UInt32`, `UInt64` | Unsigned integers | IDs, indices |
| `Float16`, `Float32`, `Float64` | Floating point numbers | Measurements, scores |
| `Decimal128`, `Decimal256` | Fixed-precision decimals | Financial data |
| `Date32`, `Date64` | Date values | Birth dates, event dates |
| `Time32`, `Time64` | Time values | Time of day |
| `Timestamp` | Date and time with timezone | Event timestamps |
| `Duration` | Time duration | Elapsed time |

### String and Binary Types

| Arrow Type | Description | Example Use Case |
|------------|-------------|------------------|
| `Utf8` | Variable-length UTF-8 string | Text, names |
| `LargeUtf8` | Large UTF-8 string (64-bit offsets) | Large documents |
| `Binary` | Variable-length binary data | Raw bytes |
| `LargeBinary` | Large binary data (64-bit offsets) | Large blobs |
| `FixedSizeBinary(n)` | Fixed-length binary data | UUIDs, hashes |

### Blob Type for Large Binary Objects

Lance provides a specialized **Blob** type for efficiently storing and retrieving very large binary objects such as videos, images, audio files, or other multimedia content. Blob columns support planned full-payload reads as well as lazy file-like access for streaming, seeking, and partial reads.

For new datasets, use blob v2 (`lance.blob.v2`) via `blob_field` and `blob_array`.

Blob versioning follows dataset file format rules:

- `data_storage_version` is the Lance file format version of a dataset.
- A dataset's `data_storage_version` is fixed once created.
- For `data_storage_version >= 2.2`, legacy blob metadata (`lance-encoding:blob`) is rejected on write.
- Legacy metadata-based blob write remains available for `0.1`, `2.0`, and `2.1`.

```python
import lance
import pyarrow as pa
from lance import blob_array, blob_field

schema = pa.schema([
    pa.field("id", pa.int64()),
    blob_field("video"),
])

table = pa.table(
    {
        "id": [1],
        "video": blob_array([b"sample-video-bytes"]),
    },
    schema=schema,
)

ds = lance.write_dataset(table, "./videos_v22.lance", data_storage_version="2.2")
_row_address, payload = ds.read_blobs("video", indices=[0])[0]
```

For legacy compatibility (`data_storage_version <= 2.1`), you can still write blob columns using `LargeBinary` with `lance-encoding:blob=true`.

To create a blob column with the legacy path, add the `lance-encoding:blob` metadata to a `LargeBinary` field:

```python
import pyarrow as pa
import lance

# Define schema with a blob column for videos
schema = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("filename", pa.utf8()),
    pa.field("video", pa.large_binary(), metadata={"lance-encoding:blob": "true"}),
])

# Read video file
with open("sample_video.mp4", "rb") as f:
    video_data = f.read()

# Create and write dataset
table = pa.table({
    "id": [1],
    "filename": ["sample_video.mp4"],
    "video": [video_data],
}, schema=schema)

ds = lance.write_dataset(
    table,
    "./videos_legacy.lance",
    schema=schema,
    data_storage_version="2.1",
)
```

To read complete blob payloads into memory, use `read_blobs()`:

```python
rows = ds.read_blobs("video", indices=[0])
_row_address, payload = rows[0]
```

Use `take_blobs()` only when you need file-like objects for lazy, partial, or seek-based reading:

```python
# Retrieve blob as a file-like object (lazy loading)
blobs = ds.take_blobs("video", indices=[0])

# Use with libraries that accept file-like objects
import av  # pip install av
with av.open(blobs[0]) as container:
    for frame in container.decode(video=0):
        # Process video frames without loading entire video into memory
        pass
```

For more details, see the [Blob API Guide](blob.md).

## Array Types for Vector Embeddings

Lance provides excellent support for array types, which are critical for storing vector embeddings in AI/ML applications.

### FixedSizeList - The Preferred Type for Vector Embeddings

`FixedSizeList` is the recommended type for storing fixed-dimensional vector embeddings. Each vector has the same number of dimensions, making it highly efficient for storage and computation.

=== "Python"

    ```python
    import lance
    import pyarrow as pa
    import numpy as np

    # Create a schema with a vector embedding column
    # This defines a 128-dimensional float32 vector
    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 128)),  # FixedSizeList of 128 floats
    ])

    # Create sample data with embeddings
    num_rows = 1000
    vectors = np.random.rand(num_rows, 128).astype(np.float32)

    table = pa.Table.from_pydict({
        "id": list(range(num_rows)),
        "text": [f"document_{i}" for i in range(num_rows)],
        "vector": [v.tolist() for v in vectors],
    }, schema=schema)

    # Write to Lance format
    ds = lance.write_dataset(table, "./embeddings.lance")
    print(f"Created dataset with {ds.count_rows()} rows")
    ```

=== "Rust"

    ```rust
    use arrow_array::{
        ArrayRef, FixedSizeListArray, Float32Array, Int64Array, RecordBatch, StringArray,
    };
    use arrow_schema::{DataType, Field, Schema};
    use lance::dataset::WriteParams;
    use lance::Dataset;
    use std::sync::Arc;

    #[tokio::main]
    async fn main() -> lance::Result<()> {
        // Define schema with a 128-dimensional vector column
        let schema = Arc::new(Schema::new(vec![
            Field::new("id", DataType::Int64, false),
            Field::new("text", DataType::Utf8, false),
            Field::new(
                "vector",
                DataType::FixedSizeList(
                    Arc::new(Field::new("item", DataType::Float32, true)),
                    128,
                ),
                false,
            ),
        ]));

        // Create sample data
        let ids = Int64Array::from(vec![0, 1, 2]);
        let texts = StringArray::from(vec!["doc_0", "doc_1", "doc_2"]);

        // Create vector embeddings (128-dimensional)
        let values: Vec<f32> = (0..384).map(|i| i as f32 / 100.0).collect();
        let values_array = Float32Array::from(values);
        let vectors = FixedSizeListArray::try_new_from_values(values_array, 128)?;

        let batch = RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(ids) as ArrayRef,
                Arc::new(texts) as ArrayRef,
                Arc::new(vectors) as ArrayRef,
            ],
        )?;

        // Write to Lance
        let dataset = Dataset::write(
            vec![batch].into_iter().map(Ok),
            "embeddings.lance",
            WriteParams::default(),
        )
        .await?;

        println!("Created dataset with {} rows", dataset.count_rows().await?);
        Ok(())
    }
    ```

### Vector Search with Embeddings

Once you have vector embeddings stored in Lance, you can perform efficient vector similarity search:

```python
import lance
import numpy as np

# Open the dataset
ds = lance.dataset("./embeddings.lance")

# Create a query vector (same dimension as stored vectors)
query_vector = np.random.rand(128).astype(np.float32).tolist()

# Perform vector search - find 10 nearest neighbors
results = ds.to_table(
    nearest={
        "column": "vector",
        "q": query_vector,
        "k": 10,
    }
)
print(results.to_pandas())
```

For production workloads with large datasets, create a vector index for much faster search:

```python
# Create an IVF-PQ index for fast approximate nearest neighbor search
ds.create_index(
    "vector",
    index_type="IVF_PQ",
    num_partitions=256,  # Number of IVF partitions
    num_sub_vectors=16,  # Number of PQ sub-vectors
)

# Search with the index (automatically used)
results = ds.to_table(
    nearest={
        "column": "vector",
        "q": query_vector,
        "k": 10,
        "nprobes": 20,  # Number of partitions to search
    }
)
```

### List and LargeList - Variable-Length Arrays

For variable-length arrays where each row may have a different number of elements, use `List` or `LargeList`:

```python
import lance
import pyarrow as pa

# Schema with variable-length arrays
schema = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("tags", pa.list_(pa.utf8())),      # Variable number of string tags
    pa.field("scores", pa.list_(pa.float32())), # Variable number of float scores
])

table = pa.Table.from_pydict({
    "id": [1, 2, 3],
    "tags": [["python", "ml"], ["rust"], ["data", "analytics", "ai"]],
    "scores": [[0.9, 0.8], [0.95], [0.7, 0.85, 0.9]],
}, schema=schema)

ds = lance.write_dataset(table, "./variable_arrays.lance")
```

## Nested and Complex Types

### Struct Types

Store structured data with multiple named fields:

```python
import lance
import pyarrow as pa

# Schema with nested struct
schema = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("metadata", pa.struct([
        pa.field("source", pa.utf8()),
        pa.field("timestamp", pa.timestamp("us")),
        pa.field("embedding_model", pa.utf8()),
    ])),
    pa.field("vector", pa.list_(pa.float32(), 384)),  # 384-dim embedding
])

table = pa.Table.from_pydict({
    "id": [1, 2],
    "metadata": [
        {"source": "web", "timestamp": "2024-01-15T10:30:00", "embedding_model": "text-embedding-3-small"},
        {"source": "api", "timestamp": "2024-01-15T11:45:00", "embedding_model": "text-embedding-3-small"},
    ],
    "vector": [
        [0.1] * 384,
        [0.2] * 384,
    ],
}, schema=schema)

ds = lance.write_dataset(table, "./with_metadata.lance")
```

### Map Types

Store key-value pairs with dynamic keys:
Map writes require Lance file format version 2.2 or later.

```python
import lance
import pyarrow as pa

schema = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("attributes", pa.map_(pa.utf8(), pa.utf8())),
])

table = pa.Table.from_pydict({
    "id": [1, 2],
    "attributes": [
        [("color", "red"), ("size", "large")],
        [("color", "blue"), ("material", "cotton")],
    ],
}, schema=schema)

ds = lance.write_dataset(table, "./with_maps.lance", data_storage_version="2.2")
```

## Data Type Mapping for Integrations

When integrating Lance with other systems (like Apache Flink, Spark, or Presto), the following type mappings apply:

| External Type | Lance/Arrow Type | Notes |
|--------------|------------------|-------|
| `BOOLEAN` | `Boolean` | |
| `TINYINT` | `Int8` | |
| `SMALLINT` | `Int16` | |
| `INT` / `INTEGER` | `Int32` | |
| `BIGINT` | `Int64` | |
| `FLOAT` | `Float32` | |
| `DOUBLE` | `Float64` | |
| `DECIMAL(p,s)` | `Decimal128(p,s)` | |
| `STRING` / `VARCHAR` | `Utf8` | |
| `CHAR(n)` | `Utf8` | Fixed-width in source system; stored as variable-length Utf8 |
| `DATE` | `Date32` | |
| `TIME` | `Time64` | Microsecond precision |
| `TIMESTAMP` | `Timestamp` | |
| `TIMESTAMP WITH LOCAL TIMEZONE` | `Timestamp` | With timezone info |
| `BINARY` / `VARBINARY` | `Binary` | |
| `BYTES` | `Binary` | |
| `BLOB` | Blob v2 extension type (`lance.blob.v2`) | Use `blob_field` / `blob_array` for new datasets; legacy metadata path applies to `data_storage_version <= 2.1` |
| `ARRAY<T>` | `List(T)` | Variable-length array |
| `ARRAY<T>(n)` | `FixedSizeList(T, n)` | Fixed-length array (vectors) |
| `ROW` / `STRUCT` | `Struct` | Nested structure |
| `MAP<K,V>` | `Map(K, V)` | Key-value pairs |

### Vector Embeddings in Integrations

For vector embedding columns, use `ARRAY<FLOAT>(n)` or `ARRAY<DOUBLE>(n)` where `n` is the embedding dimension:

```sql
-- Example: Creating a table with vector embeddings in SQL-compatible systems
CREATE TABLE embeddings (
    id BIGINT,
    text STRING,
    vector ARRAY<FLOAT>(384)  -- 384-dimensional vector
);
```

This maps to Lance's `FixedSizeList(Float32, 384)` type, which is optimized for:

- Efficient columnar storage
- SIMD-accelerated distance computations
- Vector index creation and search

## Best Practices for Vector Data

1. **Use FixedSizeList for embeddings**: Always use `FixedSizeList` (not variable-length `List`) for vector embeddings to enable efficient storage and indexing.

2. **Choose appropriate precision**:
   - `Float32` is the standard choice, balancing precision and storage
   - `Float16` or `BFloat16` can reduce storage by 50% with minimal accuracy loss
   - `Int8` for quantized embeddings

3. **Align dimensions for SIMD**: Vector dimensions divisible by 8 enable optimal SIMD acceleration. Common dimensions: 128, 256, 384, 512, 768, 1024, 1536.

4. **Create indices for large datasets**: For datasets with more than ~10,000 vectors, create an ANN index for fast search:

    ```python
    # IVF_PQ is recommended for most use cases
    ds.create_index("vector", index_type="IVF_PQ", num_partitions=256, num_sub_vectors=16)

    # IVF_HNSW_SQ offers better recall at the cost of more memory
    ds.create_index("vector", index_type="IVF_HNSW_SQ", num_partitions=256)
    ```

5. **Store metadata alongside vectors**: Lance efficiently handles mixed workloads with both vector and scalar data:

    ```python
    # Combine vector search with metadata filtering
    results = ds.to_table(
        filter="category = 'electronics'",
        nearest={"column": "vector", "q": query, "k": 10}
    )
    ```

## See Also

- [Vector Search Tutorial](../quickstart/vector-search.md) - Complete guide to vector search with Lance
- [Blob API Guide](blob.md) - Storing and retrieving large binary objects (videos, images)
- [Extension Arrays](arrays.md) - Special array types for ML (BFloat16, images)
- [Performance Guide](performance.md) - Optimization tips for large-scale deployments


================================================
FILE: docs/src/guide/distributed_indexing.md
================================================
# Distributed Indexing

!!! warning
    Lance exposes public APIs that can be integrated into an external
    distributed index build workflow, but Lance itself does not provide a full
    distributed scheduler or end-to-end orchestration layer.

    This page describes the current model, terminology, and execution flow so
    that callers can integrate these APIs correctly.

## Overview

Distributed index build in Lance follows the same high-level pattern as distributed
write:

1. multiple workers build index data in parallel
2. the caller invokes Lance segment build APIs for one distributed build
3. Lance plans and builds index artifacts from the worker outputs supplied by the caller
4. the built artifacts are committed into the dataset manifest

For vector indices and segment-native scalar indices, the worker outputs are
segments stored directly under `indices/<segment_uuid>/`. Lance can turn these
outputs into one or more physical segments and then commit them as one logical
index.

![Distributed Vector Segment Build](../images/distributed_vector_segment_build.svg)

## Terminology

This guide uses the following terms consistently:

- **Segment**: one worker output written by `execute_uncommitted()` under
  `indices/<segment_uuid>/`
- **Physical segment**: one index segment that is ready to be committed into
  the manifest
- **Logical index**: the user-visible index identified by name; a logical index
  may contain one or more physical segments

For example, a distributed vector build may create a layout like:

```text
indices/<segment_uuid_0>/
├── index.idx
└── auxiliary.idx

indices/<segment_uuid_1>/
├── index.idx
└── auxiliary.idx

indices/<segment_uuid_2>/
├── index.idx
└── auxiliary.idx
```

After segment build, Lance produces one or more segment directories:

```text
indices/<physical_segment_uuid_0>/
├── index.idx
└── auxiliary.idx

indices/<physical_segment_uuid_1>/
├── index.idx
└── auxiliary.idx
```

These physical segments are then committed together as one logical index. In the
common no-merge case, the input segments are already the physical
segments and can be committed directly.

## Roles

There are two parties involved in distributed indexing:

- **Workers** build segments
- **The caller** launches workers, chooses how those segments should be turned
  into final segments, optionally merges caller-defined groups, and commits the
  final result

Lance does not provide a distributed scheduler. The caller is responsible for
launching workers and driving the overall workflow.

## Current Model

The current model for distributed indexing has two layers of parallelism.

### Worker Build

First, multiple workers build segments in parallel:

1. on each worker, call a shard-build API such as
   `create_index_builder(...).fragments(...).execute_uncommitted()`
   or Python `create_index_uncommitted(..., fragment_ids=...)`
2. each worker writes one segment under `indices/<segment_uuid>/`

### Worker Shuffle Memory

Vector workers using the two-file IVF shuffler can set
`LANCE_SHUFFLE_MAX_PRELOADED_OFFSETS_BYTES` to control the maximum allocated bytes
retained for the shuffle offsets table. The default is `268435456` (256 MiB).
Set it before starting the shuffle; reopened shuffle readers also use the setting.
Values must be non-negative integers in bytes. `0` disables offset preloading;
invalid values return an error.

For example, allow up to 512 MiB per shuffle:

```shell
export LANCE_SHUFFLE_MAX_PRELOADED_OFFSETS_BYTES=536870912
```

When offsets fit, the reader can combine adjacent partitions into read windows.
If the limit is exceeded or allocation fails, the reader uses offsets from disk
and reads partitions individually. The limit applies to each shuffle, so memory
use adds up across concurrent builds. It does not include decoded data, index
construction, or other buffers, and it does not limit total process memory.

### Segment Merge

Then the caller decides whether those existing segments should be committed as-is
or merged into larger segments:

1. keep the worker outputs as-is and commit them directly with
   `commit_existing_index_segments(...)`, or
2. group one or more existing segments and call
   `merge_existing_index_segments(...)` for each caller-defined group
3. commit the final segment list with `commit_existing_index_segments(...)`

Within a single commit, built segments must have disjoint fragment coverage.

`merge_existing_index_segments(...)` currently supports vector, inverted,
bitmap, BTree, and zone map segments. Other scalar index families can still
commit multiple compatible segments directly when their build path supports
fragment-scoped segments, but cannot be merged into a larger physical segment
until they add a merge implementation.

### Vector Model Scope

Distributed vector builds support two model scopes.

**Shared model artifacts**: the caller trains or provides IVF centroids once and
passes the same artifacts to every worker. For IVF-PQ segments that should be
physically mergeable, workers should also use the same PQ codebook. This makes
partition ids and quantizer state have the same meaning across segments.

**Independent segment models**: each worker trains the IVF/PQ model for its own
`fragment_ids`. The resulting segments can be committed together as one logical
index without sharing centroids or codebooks.

At query time, Lance searches each physical segment independently:

1. Lance opens each segment by index UUID
2. each segment ranks IVF partitions using its own centroids
3. each segment searches the selected partitions using its own quantizer storage
4. Lance merges the candidate rows from all segments by `_distance`

Because partition ids are interpreted only within a segment during this fanout
query path, independently trained committed segments can return valid results.
For L2 and cosine IVF-PQ, each segment computes residuals against its own IVF
centroid during both build and query, so distances remain estimates of the
original query-to-vector metric.

Physical merge is a separate operation. It rewrites several segment artifacts
into one artifact with one model metadata scope. Use shared compatible model
artifacts for segments you plan to merge physically, or keep independently
trained segments as separate physical segments.

## Internal Finalize Model

Internally, Lance models distributed segment build as:

1. **build** one uncommitted segment per worker
2. **optionally merge** caller-defined groups of existing segments
3. **commit** the resulting segments as one logical index

The merge step is driven directly by the `IndexMetadata` returned from
`execute_uncommitted()`.

This is intentionally a storage-level model:

- segments are worker outputs that are not yet published
- physical segments are durable artifacts referenced by the manifest
- the logical index identity is attached only at commit time

## Segment Grouping

The caller chooses the final segment grouping:

- keep segment boundaries, so each worker output is committed directly
- merge multiple existing segments into a larger segment before commit

The grouping decision is separate from worker build. Workers only build
segments; Lance applies the segment build policy when it plans
physical segments.

## Responsibility Boundaries

The caller is expected to know:

- which distributed build is ready for segment build
- the segment metadata returned by worker builds
- how the resulting physical segments should be published

Lance is responsible for:

- writing segment artifacts
- planning physical segments from the supplied segment set
- merging segment storage into physical segment artifacts
- committing physical segments into the manifest

If a staging root or built segment directory is never committed, it remains an
unreferenced index directory under `_indices/`. These artifacts are cleaned up
by `cleanup_old_versions(...)` using the same age-based rules as other
unreferenced index files.

This split keeps distributed scheduling outside the storage engine while still
letting Lance own the on-disk index format.


================================================
FILE: docs/src/guide/distributed_write.md
================================================
# Distributed Write

!!! warning
    Lance provides out-of-the-box [Ray](https://github.com/lance-format/lance-ray) and [Spark](https://github.com/lance-format/lance-spark) integrations.

    This page is intended for users who wish to perform distributed operations in a custom manner, i.e. using `slurm` or `Kubernetes` without the Lance integration.

## Overview

The [Lance format](../format/index.md) is designed to support parallel writing across multiple distributed workers. A distributed write operation can be performed by two phases:

1. **Parallel Writes**: Generate new `lance.LanceFragment` in parallel across multiple workers.
2. **Commit**: Collect all the `lance.FragmentMetadata` and commit into a single dataset in a single `lance.LanceOperation`.

![Distributed Append](../images/distributed_append.png)

## Write new data

Writing or appending new data is straightforward with `lance.fragment.write_fragments`.

```python
import json
from lance.fragment import write_fragments

# Run on each worker
data_uri = "./dist_write"
schema = pa.schema([
    ("a", pa.int32()),
    ("b", pa.string()),
])

# Run on worker 1
data1 = {
    "a": [1, 2, 3],
    "b": ["x", "y", "z"],
}
fragments_1 = write_fragments(data1, data_uri, schema=schema)
print("Worker 1: ", fragments_1)

# Run on worker 2
data2 = {
    "a": [4, 5, 6],
    "b": ["u", "v", "w"],
}
fragments_2 = write_fragments(data2, data_uri, schema=schema)
print("Worker 2: ", fragments_2)
```

Output:
```
Worker 1:  [FragmentMetadata(id=0, files=...)]
Worker 2:  [FragmentMetadata(id=0, files=...)]
```

Now, use `lance.fragment.FragmentMetadata.to_json` to serialize the fragment metadata, and collect all serialized metadata on a single worker to execute the final commit operation.

```python
import json
from lance import FragmentMetadata, LanceOperation

# Serialize Fragments into JSON data
fragments_json1 = [json.dumps(fragment.to_json()) for fragment in fragments_1]
fragments_json2 = [json.dumps(fragment.to_json()) for fragment in fragments_2]

# On one worker, collect all fragments
all_fragments = [FragmentMetadata.from_json(f) for f in \
    fragments_json1 + fragments_json2]

# Commit the fragments into a single dataset
# Use LanceOperation.Overwrite to overwrite the dataset or create new dataset.
op = lance.LanceOperation.Overwrite(schema, all_fragments)
read_version = 0 # Because it is empty at the time.
lance.LanceDataset.commit(
    data_uri,
    op,
    read_version=read_version,
)

# We can read the dataset using the Lance API:
dataset = lance.dataset(data_uri)
assert len(dataset.get_fragments()) == 2
assert dataset.version == 1
print(dataset.to_table().to_pandas())
```

Output:
```
     a  b
0  1  x
1  2  y
2  3  z
3  4  u
4  5  v
5  6  w
```

## Append data

Appending additional data follows a similar process. Use `lance.LanceOperation.Append` to commit the new fragments, ensuring that the `read_version` is set to the current dataset's version.

```python
import lance

ds = lance.dataset(data_uri)
read_version = ds.version # record the read version

op = lance.LanceOperation.Append(all_fragments)
lance.LanceDataset.commit(
    data_uri,
    op,
    read_version=read_version,
)
```

## Add New Columns

[Lance Format excels at operations such as adding columns](../format/index.md). Thanks to its two-dimensional layout ([see this blog post](https://blog.lancedb.com/designing-a-table-format-for-ml-workloads/)), adding new columns is highly efficient since it avoids copying the existing data files. Instead, the process simply creates new data files and links them to the existing dataset using metadata-only operations.

```python
import lance
from pyarrow import RecordBatch
import pyarrow.compute as pc

dataset = lance.dataset("./add_columns_example")
assert len(dataset.get_fragments()) == 2
assert dataset.to_table().combine_chunks() == pa.Table.from_pydict({
    "name": ["alice", "bob", "charlie", "craig", "dave", "eve"],
    "age": [25, 33, 44, 55, 66, 77],
}, schema=schema)


def name_len(names: RecordBatch) -> RecordBatch:
    return RecordBatch.from_arrays(
        [pc.utf8_length(names["name"])],
        ["name_len"],
    )

# On Worker 1
frag1 = dataset.get_fragments()[0]
new_fragment1, new_schema = frag1.merge_columns(name_len, ["name"])

# On Worker 2
frag2 = dataset.get_fragments()[1]
new_fragment2, _ = frag2.merge_columns(name_len, ["name"])

# On Worker 3 - Commit
all_fragments = [new_fragment1, new_fragment2]
op = lance.LanceOperation.Merge(all_fragments, schema=new_schema)
lance.LanceDataset.commit(
    "./add_columns_example",
    op,
    read_version=dataset.version,
)

# Verify dataset
dataset = lance.dataset("./add_columns_example")
print(dataset.to_table().to_pandas())
```

Output:
```
      name  age  name_len
0    alice   25         5
1      bob   33         3
2  charlie   44         7
3    craig   55         5
4     dave   66         4
5      eve   77         3
```

## Update Columns

Currently, Lance supports the fragment level update columns ability to update existing columns in a distributed manner.

This operation performs a left-outer-hash-join with the right table (new data)
on the column specified by `left_on` and `right_on`. For every row in the current
fragment, the updated column value is:
1. If no matched row on the right side, the column value of the left side row.
2. If there is exactly one corresponding row on the right side, the column value
   of the matching row.
3. If there are multiple corresponding rows, the column value of a random row.

```python
import lance
import pyarrow as pa

# Create initial dataset with two fragments
# First fragment
data1 = pa.table(
    {
        "id": [1, 2, 3, 4],
        "name": ["Alice", "Bob", "Charlie", "David"],
        "score": [85, 90, 75, 80],
    }
)
dataset_uri = "./my_dataset.lance"
dataset = lance.write_dataset(data1, dataset_uri)

# Second fragment
data2 = pa.table(
    {
        "id": [5, 6, 7, 8],
        "name": ["Eve", "Frank", "Grace", "Henry"],
        "score": [88, 92, 78, 82],
    }
)
dataset = lance.write_dataset(data2, dataset_uri, mode="append")

# Prepare update data for fragment 0 using 'id' as join key
update_data1 = pa.table(
    {
        "id": [1, 3],
        "name": ["Alan", "Chase"],
        "score": [95, 85],
    }
)

# Prepare update data for fragment 1
update_data2 = pa.table(
    {
        "id": [5, 7],
        "name": ["Eva", "Gracie"],
        "score": [98, 88],
    }
)

# Update fragment 0
fragment0 = dataset.get_fragment(0)
updated_fragment0, fields_modified0 = fragment0.update_columns(
    update_data1, left_on="id", right_on="id"
)

# Update fragment 1
fragment1 = dataset.get_fragment(1)
updated_fragment1, fields_modified1 = fragment1.update_columns(
    update_data2, left_on="id", right_on="id"
)

union_fields_modified = list(set(fields_modified0 + fields_modified1))
# Commit the changes for both fragments
op = lance.LanceOperation.Update(
    updated_fragments=[updated_fragment0, updated_fragment1],
    fields_modified=union_fields_modified,
)
updated_dataset = lance.LanceDataset.commit(
    str(dataset_uri), op, read_version=dataset.version
)

# Verify the update
dataset = lance.dataset(dataset_uri)
print(dataset.to_table().to_pandas())
```

Output:
```
   id    name  score
0   1    Alan     95
1   2     Bob     90
2   3   Chase     85
3   4   David     80
4   5     Eva     98
5   6   Frank     92
6   7  Gracie     88
7   8   Henry     82
```

### Handling stable row id

On a dataset created with `enable_stable_row_ids=True`, each row keeps the same
`_rowid` for its lifetime, even when an update rewrites it into a different
fragment. Lance cannot infer which new row replaces which old one, so when you
assemble the transaction yourself, carrying those ids across is your job: read
the rows you are rewriting with `with_row_id=True` and attach their ids to the
new fragment with `lance.fragment.RowIdSequence`.

Rows you leave without an id are treated as newly inserted. That is not an error,
so a fragment written without `row_id_meta` commits successfully while silently
giving every rewritten row a fresh identity, breaking `_rowid` for anything
downstream that relies on it.

You do **not** need to supply `created_at_version_meta` or
`last_updated_at_version_meta`. Leave them as `None`. Lance derives both while
building the manifest: `last_updated_at_version_meta` becomes the version being
committed, and `created_at_version_meta` is copied from whichever existing row
carries the same stable row id, so a rewritten row keeps the version it first
appeared in.

#### Mixing updated and new rows

A single fragment may hold both rewritten rows and brand new ones. Order it so
that **the rewritten rows come first and the new rows last**, then pass only the
row ids of the rewritten rows. The row ids bind to the leading rows in fragment
order, and the commit generates new ids for the remaining rows.

Do not generate ids for the new rows yourself. Row ids are handed out from a counter
in the manifest, and a commit that loses a race is retried against the version
that won, which may have consumed the very ids you picked. Only the commit knows
which values are free, so it assigns them after conflict resolution has settled.
Supplying more row ids than the fragment has rows is rejected.

```python
import lance
import pyarrow as pa
import pyarrow.compute as pc
from lance.fragment import RowIdSequence, write_fragments

schema = pa.schema([("id", pa.int64()), ("score", pa.int64())])
dataset_uri = "./stable_row_ids.lance"
dataset = lance.write_dataset(
    pa.table({"id": [1, 2, 3, 4], "score": [85, 90, 75, 80]}, schema=schema),
    dataset_uri,
    enable_stable_row_ids=True,
)

# On a worker: read the rows to rewrite, keeping their stable row ids.
rows = dataset.to_table(columns=["id", "score"], with_row_id=True)
rewritten = rows.filter(pc.field("id").isin([2, 3]))

# Rewritten rows first, then the row that did not exist before.
new_data = pa.table(
    {
        "id": rewritten["id"].to_pylist() + [5],
        "score": [95, 70, 60],
    },
    schema=schema,
)
fragments = write_fragments(new_data, dataset_uri, schema=schema)
assert len(fragments) == 1

# Only the rewritten rows have ids. The trailing row gets one at commit time.
fragments[0].row_id_meta = RowIdSequence(rewritten["_rowid"]).to_inline_metadata()

# On the committing worker: tombstone the old copies of the rewritten rows.
updated_fragment = dataset.get_fragments()[0].delete("id in (2, 3)")

op = lance.LanceOperation.Update(
    updated_fragments=[updated_fragment],
    new_fragments=fragments,
)
dataset = lance.LanceDataset.commit(dataset_uri, op, read_version=dataset.version)

print(dataset.to_table(with_row_id=True).to_pandas())
```

Output:
```
   id  score  _rowid
0   1     85       0
1   4     80       3
2   2     95       1
3   3     70       2
4   5     60       4
```

Row ids 1 and 2 followed their rows into the new fragment, and the inserted row
received the next unused id. Reading the lineage columns shows that the rewritten
rows kept their original creation version while the inserted row is stamped with
the version that added it:

```python
print(
    dataset.to_table(
        columns=["id", "_row_created_at_version", "_row_last_updated_at_version"]
    ).to_pandas()
)
```

Output:
```
   id  _row_created_at_version  _row_last_updated_at_version
0   1                        1                             1
1   4                        1                             1
2   2                        1                             2
3   3                        1                             2
4   5                        2                             2
```


================================================
FILE: docs/src/guide/json.md
================================================
# JSON Support

Lance provides comprehensive support for storing and querying JSON data, enabling you to work with semi-structured data efficiently. This guide covers how to store JSON data in Lance datasets and use JSON functions to query and filter your data.

## Getting Started

```python
import lance
import pyarrow as pa
import json

# Create a table with JSON data
json_data = {"name": "Alice", "age": 30, "city": "New York"}
json_arr = pa.array([json.dumps(json_data)], type=pa.json_())
table = pa.table({"id": [1], "data": json_arr})

# Write the dataset
lance.write_dataset(table, "dataset.lance")
```

## Storage Format

Lance stores JSON data internally as JSONB (binary JSON) using the `lance.json` extension type. This provides:

- Efficient storage through binary encoding
- Fast query performance for nested field access
- Compatibility with Apache Arrow's JSON type

When you read JSON data back from Lance, it's automatically converted to Arrow's JSON type for seamless integration with your data processing pipelines.

## JSON Functions

Lance provides a comprehensive set of JSON functions for querying and filtering JSON data. These functions can be used in filter expressions with methods like `to_table()`, `scanner()`, and SQL queries through DataFusion integration.

### Data Access Functions

#### json_extract

Extracts a value from JSON using JSONPath syntax.

**Syntax:** `json_extract(json_column, json_path)`

**Returns:** JSON-formatted string representation of the extracted value

**Example:**
```python
# Sample data: {"user": {"name": "Alice", "age": 30}}
result = dataset.to_table(
    filter="json_extract(data, '$.user.name') = '\"Alice\"'"
)
# Returns: "\"Alice\"" for strings, "30" for numbers, "true" for booleans
```

!!! note
    `json_extract` returns values in JSON format. String values include quotes (e.g., `"Alice"`),
    numbers are returned as-is (e.g., `30`), and booleans as `true`/`false`.

#### json_get

Retrieves a field or array element from JSON, returning it as JSONB for further processing.

**Syntax:** `json_get(json_column, key_or_index)`

**Parameters:**
- `key_or_index`: Field name (string) or array index (numeric string like "0", "1")

**Returns:** JSONB binary value (can be used for nested access)

**Example:**
```python
# Access nested JSON by chaining json_get calls
# Sample data: {"user": {"profile": {"name": "Alice"}}}
result = dataset.to_table(
    filter="json_get_string(json_get(json_get(data, 'user'), 'profile'), 'name') = 'Alice'"
)

# Access array elements by index
# Sample data: ["first", "second", "third"]
result = dataset.to_table(
    filter="json_get_string(data, '0') = 'first'"  # Gets first array element
)
```

### Type-Safe Value Extraction

These functions extract values with strict type conversion. The conversion uses JSONB's built-in strict mode, which requires values to be of compatible types:

#### json_get_string

Extracts a string value from JSON.

**Syntax:** `json_get_string(json_column, key_or_index)`

**Parameters:**
- `key_or_index`: Field name or array index (as string)

**Returns:** String value (without JSON quotes), null if conversion fails

**Type Conversion:** Uses strict conversion - numbers and booleans are converted to their string representation

**Example:**
```python
result = dataset.to_table(
    filter="json_get_string(data, 'name') = 'Alice'"
)

# Array access example
# Sample data: ["first", "second"]
result = dataset.to_table(
    filter="json_get_string(data, '1') = 'second'"  # Gets second array element
)
```

#### json_get_int

Extracts an integer value with strict type conversion.

**Syntax:** `json_get_int(json_column, key_or_index)`

**Returns:** 64-bit integer, null if conversion fails

**Type Conversion:** Uses JSONB's strict `to_i64()` conversion:
- Numbers are truncated to integers
- Strings must be parseable as numbers
- Booleans: true → 1, false → 0

**Example:**
```python
# {"age": 30} works, {"age": "30"} may work if JSONB allows string parsing
result = dataset.to_table(
    filter="json_get_int(data, 'age') > 25"
)
```

#### json_get_float

Extracts a floating-point value with strict type conversion.

**Syntax:** `json_get_float(json_column, key_or_index)`

**Returns:** 64-bit float, null if conversion fails

**Type Conversion:** Uses JSONB's strict `to_f64()` conversion:
- Integers are converted to floats
- Strings must be parseable as numbers
- Booleans: true → 1.0, false → 0.0

**Example:**
```python
result = dataset.to_table(
    filter="json_get_float(data, 'score') >= 90.5"
)
```

#### json_get_bool

Extracts a boolean value with strict type conversion.

**Syntax:** `json_get_bool(json_column, key_or_index)`

**Returns:** Boolean, null if conversion fails

**Type Conversion:** Uses JSONB's strict `to_bool()` conversion:
- Numbers: 0 → false, non-zero → true
- Strings: "true" → true, "false" → false (exact match required)
- Other values may fail conversion

**Example:**
```python
result = dataset.to_table(
    filter="json_get_bool(data, 'active') = true"
)
```

### Existence and Array Functions

#### json_exists

Checks if a JSONPath exists in the JSON data.

**Syntax:** `json_exists(json_column, json_path)`

**Returns:** Boolean

**Example:**
```python
# Find records that have an age field
result = dataset.to_table(
    filter="json_exists(data, '$.user.age')"
)
```

#### json_array_contains

Checks if a JSON array contains a specific value.

**Syntax:** `json_array_contains(json_column, json_path, value)`

**Returns:** Boolean

**Comparison Logic:**
- Compares array elements as JSON strings
- For string matching, tries both with and without quotes
- Example: searching for 'python' matches both `"python"` and `python` in the array

**Example:**
```python
# Sample data: {"tags": ["python", "ml", "data"]}
result = dataset.to_table(
    filter="json_array_contains(data, '$.tags', 'python')"
)
```

#### json_array_length

Returns the length of a JSON array.

**Syntax:** `json_array_length(json_column, json_path)`

**Returns:**
- Integer: length of the array
- null: if path doesn't exist
- Error: if path points to a non-array value

**Example:**
```python
# Find records with more than 3 tags
result = dataset.to_table(
    filter="json_array_length(data, '$.tags') > 3"
)

# Empty arrays return 0
result = dataset.to_table(
    filter="json_array_length(data, '$.empty_array') = 0"
)
```

## JSON Indexing

Lance supports indexing JSON columns to accelerate filters on frequently queried paths.

### Scalar Index on a JSON Path

For `pa.json_()` columns, create a scalar index with `IndexConfig` and specify the JSON
path to index. The query should use the same path literal that was indexed.

```python
import json
import lance
import pyarrow as pa
from lance.indices import IndexConfig

table = pa.table({
    "id": [1, 2, 3, 4],
    "data": pa.array([
        json.dumps({"x": 7, "y": 10}),
        json.dumps({"x": 11, "y": 22}),
        json.dumps({"y": 0}),
        json.dumps({"x": 10}),
    ], type=pa.json_()),
})

lance.write_dataset(table, "json-index.lance")
dataset = lance.dataset("json-index.lance")

dataset.create_scalar_index(
    "data",
    IndexConfig(
        index_type="json",
        parameters={
            "target_index_type": "btree",
            "path": "x",
        },
    ),
)

result = dataset.to_table(filter="json_get_int(data, 'x') = 10")
```

!!! note
    The JSON index matches queries by path literal. For example, if the index is built
    with `path="x"`, then the filter should also use `"x"` with a function such as
    `json_get_int(data, 'x')`. If the index is built with `path="$.user.name"`, then
    the filter should use `json_extract(data, '$.user.name')`.

### Full-Text Search on JSON Documents

If you want text search over the contents of a JSON document instead of scalar filtering
on a single path, create an `INVERTED` index on the JSON column.

```python
dataset.create_scalar_index(
    "data",
    index_type="INVERTED",
    base_tokenizer="simple",
    lower_case=True,
    stem=True,
    remove_stop_words=True,
)
```

!!! note
    JSON columns and nested struct columns are indexed differently. For nested struct
    fields, use dot notation such as `meta.lang`. For `pa.json_()` columns, use the JSON
    index shown above and query with `json_get_*` or `json_extract`.

## Usage Examples

### Working with Nested JSON

```python
import lance
import pyarrow as pa
import json

# Create nested JSON data
data = [
    {
        "id": 1,
        "user": {
            "profile": {
                "name": "Alice",
                "settings": {
                    "theme": "dark",
                    "notifications": True
                }
            },
            "scores": [95, 87, 92]
        }
    },
    {
        "id": 2,
        "user": {
            "profile": {
                "name": "Bob",
                "settings": {
                    "theme": "light",
                    "notifications": False
                }
            },
            "scores": [88, 91, 85]
        }
    }
]

# Convert to Lance dataset
json_strings = [json.dumps(d) for d in data]
table = pa.table({
    "data": pa.array(json_strings, type=pa.json_())
})

lance.write_dataset(table, "nested.lance")
dataset = lance.dataset("nested.lance")

# Query nested fields using JSONPath
dark_theme_users = dataset.to_table(
    filter="json_extract(data, '$.user.profile.settings.theme') = '\"dark\"'"
)

# Or using chained json_get
high_scorers = dataset.to_table(
    filter="json_array_length(data, '$.user.scores') >= 3"
)
```

### Combining JSON with Other Data Types

```python
# Create mixed-type table with JSON metadata
products = pa.table({
    "id": [1, 2, 3],
    "name": ["Laptop", "Phone", "Tablet"],
    "price": [999.99, 599.99, 399.99],
    "specs": pa.array([
        json.dumps({"cpu": "i7", "ram": 16, "storage": 512}),
        json.dumps({"screen": 6.1, "battery": 4000, "5g": True}),
        json.dumps({"screen": 10.5, "battery": 7000, "stylus": True})
    ], type=pa.json_())
})

lance.write_dataset(products, "products.lance")
dataset = lance.dataset("products.lance")

# Find products with specific specs
result = dataset.to_table(
    filter="price < 600 AND json_get_bool(specs, '5g') = true"
)
```

### Handling Arrays in JSON

```python
# Create data with JSON arrays
records = pa.table({
    "id": [1, 2, 3],
    "data": pa.array([
        json.dumps({"name": "Project A", "tags": ["python", "ml", "production"]}),
        json.dumps({"name": "Project B", "tags": ["rust", "systems"]}),
        json.dumps({"name": "Project C", "tags": ["python", "web", "api", "production"]})
    ], type=pa.json_())
})

lance.write_dataset(records, "projects.lance")
dataset = lance.dataset("projects.lance")

# Find projects with Python
python_projects = dataset.to_table(
    filter="json_array_contains(data, '$.tags', 'python')"
)

# Find projects with more than 3 tags
complex_projects = dataset.to_table(
    filter="json_array_length(data, '$.tags') > 3"
)
```

## Performance Considerations

1. **Choose the right function**: Use `json_get_*` functions for direct field access and type conversion; use `json_extract` for complex JSONPath queries.
2. **Index frequently queried paths**: Use a JSON scalar index on frequently filtered paths before creating computed columns for the same fields.
3. **Minimize deep nesting**: While Lance supports arbitrary nesting, flatter structures generally perform better.
4. **Understand type conversion**: The `json_get_*` functions use strict type conversion, which may fail if types don't match. Plan your schema accordingly.
5. **Array access**: When working with JSON arrays, you can access elements by index using numeric strings (e.g., "0", "1") with `json_get` functions.

## Integration with DataFusion

All JSON functions are available when using Lance with Apache DataFusion for SQL queries. See the [DataFusion Integration](../integrations/datafusion.md#json-functions) guide for more details on using JSON functions in SQL contexts.

## Limitations

- JSONPath support follows standard JSONPath syntax but may not support all advanced features
- Large JSON documents may impact query performance
- JSON functions are currently only available for filtering, not for projection in query results


================================================
FILE: docs/src/guide/migration.md
================================================
# Migration Guides

Lance aims to avoid breaking changes when possible.  Currently, we are refining the Rust public API so that we can move
it out of experimental status and make stronger commitments to backwards compatibility.  The python API is considered
stable and breaking changes should generally be communicated (via warnings) for 1-2 months prior to being finalized to
give users a chance to migrate.  This page documents the breaking changes between releases and gives advice on how to
migrate.

## 9.0.0

* Unless overridden, newly created FTS indexes use format v2. The code analyzer
  and `block_size=256` require format v3, so readers must support v3 before an
  index using either option is created. `document_granularity="list_element"`
  also requires v3 reader capability, independently of the posting format.

* To keep new indexes readable by nodes that support at most format v1 or v2,
  set `format_version` in the index creation parameters, or set
  `LANCE_FTS_FORMAT_VERSION` for a rollout-wide override. Formats v1 and v2
  require the text analyzer and `block_size=128`.

* Operations that maintain an existing FTS index, including append, incremental
  indexing, optimize, and mem-wal maintained-index flush, preserve its format
  version.

## 7.2.0

* The `IndexSegmentBuilder` API has been removed from Rust, Python, and Java.
  This API was deprecated by the distributed indexing flow based on
  `create_index_uncommitted`, `merge_existing_index_segments`, and
  `commit_existing_index_segments`, but remained in the codebase as a parallel
  way to plan and publish staged index segments.

* Callers should now publish staged segment outputs directly with
  `commit_existing_index_segments(...)`. If multiple staged outputs should be
  combined into a larger physical segment first, callers should explicitly group
  those outputs and call `merge_existing_index_segments(...)` for each group
  before committing the final segment list.

* The old builder's `target_segment_bytes` automatic size-based grouping has no
  direct replacement. Distributed index drivers that used it should choose
  segment groups themselves, then pass each group to
  `merge_existing_index_segments(...)`.

## 5.0.0

* The default data storage version changed from 2.0 to 2.1. This affects the `column_indices`
  field in the `DataFile` protobuf message. In 2.0, every field (including non-leaf fields like
  struct containers and list containers) was assigned a sequential column index. In 2.1, non-leaf
  fields (unpacked structs, list containers) are assigned `-1` instead since their validity
  information is now folded into repetition/definition levels. Only leaf fields and packed structs
  are assigned column indices.

    For example, given the schema:

    ```
    x: i32, y: [f32], z: { a: i32 }
    ```

    The fields (in depth-first order) are:

    | Field ID | Field         |
    |----------|---------------|
    | 0        | `x` (i32)     |
    | 1        | `y` (list)    |
    | 2        | `y.item` (f32)|
    | 3        | `z` (struct)  |
    | 4        | `z.a` (i32)   |

    In **2.0**, `column_indices` = `[0, 1, 2, 3, 4]` — every field gets a column.

    In **2.1**, `column_indices` = `[0, -1, 1, -1, 2]` — non-leaf fields (`y` and `z`) get `-1`.

* This change only affects advanced users who construct `DataFile` messages directly, for example
  when building operations by hand for `Dataset.commit`. Normal read and write paths are
  unaffected.

* To opt back to 2.0 format, set `data_storage_version="2.0"` when creating a dataset.

## 1.0.0

* The `SearchResult` returned by scalar indices must now output information about null values.
  Instead of containing a `RowIdTreeMap`, it now contains a `NullableRowIdSet`. Expressions that
  resolve to null values must be included in search results in the null set. This ensures that
  `NOT` can be applied to index search results correctly.

## 0.39

* The `lance` crate no longer re-exports utilities from `lance-arrow` such as `RecordBatchExt` or `SchemaExt`.  In the
short term, if you are relying on these utilities,  you can add a dependency on the `lance-arrow` crate.  However, we
do not expect `lance-arrow` to ever be stable, and you may want to consider forking these utilities.

* Previously, we exported `Error` and `Result` as both `lance::Error` and `lance::error::Error`.  We have now reduced
this to just `lance::Error`.  We have also removed some internal error utilities (such as `OptionExt`) from the public
API and do not plan on reintroducing these.

* The Python and Rust `dataset::diff_meta` API has been removed in favor of `dataset::delta`,
which returns a `DatasetDelta` that offers both metadata diff through `list_transactions` and data diff
through `get_inserted_rows` and `get_updated_rows`.

* Some other minor utilities which had previously been public are now private.  It is unlikely anyone was utilizing'
these.  Please open an issue if you were relying on any of these.

* The `lance-namespace` Rust crate now splits into `lance-namespace` that contains the main `LanceNamespace` trait
and data models, and `lance-namespace-impls` that has different implementations of the namespace.
The `DirectoryNamespace` and `RestNamespace` interfaces have been refactored to be more user friendly.
The `DirectoryNamespace` also now uses Lance ObjectStore for IO instead of directly depending on Apache OpenDAL.


================================================
FILE: docs/src/guide/object_store.md
================================================
# Object Store Configuration

Lance supports object stores such as AWS S3 (and compatible stores), Azure Blob Store,
and Google Cloud Storage. Which object store to use is determined by the URI scheme of
the dataset path. For example, `s3://bucket/path` will use S3, `az://bucket/path`
will use Azure, and `gs://bucket/path` will use GCS.

These object stores take additional configuration objects. There are two ways to
specify these configurations: by setting environment variables or by passing them
to the `storage_options` parameter of `lance.dataset` and
`lance.write_dataset`. So for example, to globally set a higher timeout,
you would run in your shell:

```bash
export TIMEOUT=60s
```

If you only want to set the timeout for a single dataset, you can pass it as a
storage option:

```python
import lance
ds = lance.dataset("s3://path", storage_options={"timeout": "60s"})
```

## General Configuration

These options apply to all object stores.

| Key                          | Description                                                                                                                                                                                                                                                                                             |
|------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `allow_http`                 | Allow non-TLS, i.e. non-HTTPS connections. Default, `False`.                                                                                                                                                                                                                                            |
| `block_size`                 | Gap in bytes below which two reads of one file are merged into a single request. Default, `4096` for local files and `65536` for object stores. Larger values cut the request count of scattered reads (for example a `take` of rows spread across a file) at the cost of reading the bytes in between.                                                                                                     |
| `download_retry_count`       | Number of times to retry a download. Default, `3`. This limit is applied when the HTTP request succeeds but the response is not fully downloaded, typically due to a violation of `timeout`.                                                                                                            |
| `allow_invalid_certificates` | Skip certificate validation on https connections. Default, `False`. Warning: This is insecure and should only be used for testing.                                                                                                                                                                      |
| `connect_timeout`            | Timeout for only the connect phase of a Client. Default, `5s`.                                                                                                                                                                                                                                          |
| `timeout`                    | Timeout for the entire request, from connection until the response body has finished. Default, `30s`. This applies to each individual request, so on a large write it must cover one complete multipart part upload; raise it alongside `LANCE_INITIAL_UPLOAD_SIZE`.                                    |
| `user_agent`                 | User agent string to use in requests.                                                                                                                                                                                                                                                                   |
| `proxy_url`                  | URL of a proxy server to use for requests. Default, `None`.                                                                                                                                                                                                                                             |
| `proxy_ca_certificate`       | PEM-formatted CA certificate for proxy connections                                                                                                                                                                                                                                                      |
| `proxy_excludes`             | List of hosts that bypass proxy. This is a comma separated list of domains and IP masks. Any subdomain of the provided domain will be bypassed. For example, `example.com, 192.168.1.0/24` would bypass `https://api.example.com`, `https://www.example.com`, and any IP in the range `192.168.1.0/24`. |
| `client_max_retries`         | Number of times for the object store client to retry the request. Default, `3`.                                                                                                                                                                                                                         |
| `client_retry_timeout`       | Timeout for the object store client to retry the request in seconds. Default, `180`.                                                                                                                                                                                                                    |

### Bulk copy strategy

Lance streams bulk index-file movement and dataset deep-clone files through
read and write APIs by default. This avoids requiring a provider-native copy
operation and works across different object stores.

Set `LANCE_IO_SERVER_SIDE_COPY_ENABLED` to a truthy value (`1`, `true`, `on`,
`yes`, or `y`, case-insensitive) to opt cloud copies whose source and destination
share the same object-store client into the provider-native server-side copy
operation. Cross-client, cross-store, and local copies do not use this setting.
Native copy can reduce client bandwidth and transfer cost, but it requires copy
support from the object-store integration and is subject to the provider
request's timeout and retry behavior.

Deep clone bounds non-local file movement to four concurrent files by default.
Set `LANCE_DEEP_CLONE_STREAM_CONCURRENCY` to a positive integer to override this
operation-specific limit. The bound also applies when server-side copy is
enabled because S3 and GCS copies above the provider's single-copy size limit
fall back to streaming through Lance.

## Per-Base Configuration

A dataset can register additional base paths that store part of its data, and each
base may live in a different bucket, account, or storage provider. A storage option
key of the form `base_<id>.<key>` applies `<key>` only to the base path with that
manifest id. Every base inherits the unscoped options; base-scoped entries add to or
override them for that base only.

```python
import lance
ds = lance.dataset(
    "az://account-a/path",
    storage_options={
        # Shared defaults, used by the primary dataset and inherited by bases
        "account_name": "account-a",
        "account_key": "key-a",
        # Overrides for the base path with id 1
        "base_1.account_name": "account-b",
        "base_1.account_key": "key-b",
    },
)
```

Base ids are assigned when bases are registered (`initial_bases` ids are assigned
sequentially starting at 1, in order) and can be inspected with
`ds.base_paths()`. The returned dictionary maps each base id to its registered
`DatasetBasePath`; its iteration order is unspecified, and it does not include the
primary storage unless that path was explicitly registered as a base.

Keys that do not match `base_<id>.<key>` exactly (e.g. `base_url`) are treated as
regular storage options. Exact per-base parameter maps (`base_store_params`, keyed
by base path URI) take precedence over base-scoped keys for that base.

## S3 Configuration

S3 (and S3-compatible stores) have additional configuration options that configure
authorization and S3-specific features (such as server-side encryption).

AWS credentials can be set in the environment variables `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`. Alternatively, they can be
passed as parameters to the `storage_options` parameter:

```python
import lance
ds = lance.dataset(
    "s3://bucket/path",
    storage_options={
        "access_key_id": "my-access-key",
        "secret_access_key": "my-secret-key",
        "session_token": "my-session-token",
    }
)
```

If you are using AWS SSO, you can specify the `AWS_PROFILE` environment variable.
It cannot be specified in the `storage_options` parameter.

The following keys can be used as both environment variables or keys in the
`storage_options` parameter:

| Key                                                                 | Description                                                                                                                                      |
|---------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `aws_region` / `region`                                             | The AWS region the bucket is in. This can be automatically detected when using AWS S3, but must be specified for S3-compatible stores.           |
| `aws_access_key_id` / `access_key_id`                               | The AWS access key ID to use.                                                                                                                    |
| `aws_secret_access_key` / `secret_access_key`                       | The AWS secret access key to use.                                                                                                                |
| `aws_session_token` / `session_token`                               | The AWS session token to use.                                                                                                                    |
| `aws_endpoint` / `endpoint`                                         | The endpoint to use for S3-compatible stores.                                                                                                    |
| `aws_virtual_hosted_style_request` / `virtual_hosted_style_request` | Whether to use virtual hosted-style requests, where bucket name is part of the endpoint. Meant to be used with `aws_endpoint`. Default, `False`. |
| `aws_s3_express` / `s3_express`                                     | Whether to use S3 Express One Zone endpoints. Default, `False`. See more details below.                                                          |
| `aws_server_side_encryption`                                        | The server-side encryption algorithm to use. Must be one of `"AES256"`, `"aws:kms"`, or `"aws:kms:dsse"`. Default, `None`.                       |
| `aws_sse_kms_key_id`                                                | The KMS key ID to use for server-side encryption. If set, `aws_server_side_encryption` must be `"aws:kms"` or `"aws:kms:dsse"`.                  |
| `aws_sse_bucket_key_enabled`                                        | Whether to use bucket keys for server-side encryption.                                                                                           |

### Credential provider selection

By default, Lance uses the standard AWS credential provider chain (environment
variables, shared config file, web identity tokens, ECS, EC2 instance metadata).

The `aws_provider_scheme` storage option pins a dataset to a specific credential
provider, which is useful when two datasets in the same process need different
AWS auth (for example, one bucket using IRSA and another using ECS container
credentials).

| Value | Behavior |
|-------|----------|
| `token` | Use static access-key credentials. Returns an error if `aws_access_key_id` and `aws_secret_access_key` are not set. |
| `ecs` | Use the ECS/Pod Identity container credential endpoint. Reads `AWS_CONTAINER_CREDENTIALS_FULL_URI` or `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` from the environment. |
| `irsa` | Use IRSA (IAM Roles for Service Accounts) web identity token credentials. Reads `AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_ROLE_ARN` from the environment. |

```python
import lance

# Bucket A — use IRSA (web identity token from the environment)
ds_a = lance.dataset(
    "s3://bucket-a/path",
    storage_options={"aws_provider_scheme": "irsa"},
)

# Bucket B — use ECS container credentials
ds_b = lance.dataset(
    "s3://bucket-b/path",
    storage_options={"aws_provider_scheme": "ecs"},
)
```

### S3-compatible stores

Lance can also connect to S3-compatible stores, such as MinIO. To do so, you must
specify both region and endpoint:

```python
import lance
ds = lance.dataset(
    "s3://bucket/path",
    storage_options={
        "region": "us-east-1",
        "endpoint": "http://minio:9000",
    }
)
```

This can also be done with the `AWS_ENDPOINT` and `AWS_DEFAULT_REGION` environment variables.

### S3 Express (Directory Bucket)

Lance supports [S3 Express One Zone](https://aws.amazon.com/s3/storage-classes/express-one-zone/) buckets,
a.k.a. S3 directory buckets.
S3 Express buckets only support connecting from an EC2 instance within the same
region.
By default, Lance automatically recognize the `--x-s3` suffix of an express bucket,
there is no special configuration needed.

In case of an access point or private link that hides the bucket name,
you can configure express bucket access explicitly through storage option `s3_express`.

```python
import lance
ds = lance.dataset(
    "s3://my-bucket--use1-az4--x-s3/path/imagenet.lance",
    storage_options={
        "region": "us-east-1",
        "s3_express": "true",
    }
)
```

## Google Cloud Storage Configuration

GCS credentials are configured by setting the `GOOGLE_SERVICE_ACCOUNT` environment
variable to the path of a JSON file containing the service account credentials.
Alternatively, you can pass the path to the JSON file in the `storage_options`

```python
import lance
ds = lance.dataset(
    "gs://my-bucket/my-dataset",
    storage_options={
        "service_account": "path/to/service-account.json",
    }
)
```

!!! note

    By default, GCS uses HTTP/1 for communication, as opposed to HTTP/2. This improves
    maximum throughput significantly. However, if you wish to use HTTP/2 for some reason,
    you can set the environment variable `HTTP1_ONLY` to `false`.

The following keys can be used as both environment variables or keys in the
`storage_options` parameter:

| Key | Description |
|-----|-------------|
| `google_service_account` / `service_account` | Path to the service account JSON file. |
| `google_service_account_key` / `service_account_key` | The serialized service account key. |
| `google_application_credentials` / `application_credentials` | Path to the application credentials. |

## Azure Blob Storage Configuration

Azure Blob Storage credentials can be configured by setting the `AZURE_STORAGE_ACCOUNT_NAME`
and `AZURE_STORAGE_ACCOUNT_KEY` environment variables. Alternatively, you can pass
the account name and key in the `storage_options` parameter:

```python
import lance
ds = lance.dataset(
    "az://my-container/my-dataset",
    storage_options={
        "account_name": "some-account",
        "account_key": "some-key",
    }
)
```

These keys can be used as both environment variables or keys in the `storage_options` parameter:

| Key | Description |
|-----|-------------|
| `azure_storage_account_name` / `account_name` | The name of the azure storage account. |
| `azure_storage_account_key` / `account_key` | The serialized service account key. |
| `azure_client_id` / `client_id` | Service principal client id for authorizing requests. |
| `azure_client_secret` / `client_secret` | Service principal client secret for authorizing requests. |
| `azure_tenant_id` / `tenant_id` | Tenant id used in oauth flows. |
| `azure_storage_sas_key` / `azure_storage_sas_token` / `sas_key` / `sas_token` | Shared access signature. The signature is expected to be percent-encoded, much like they are provided in the azure storage explorer or azure portal. |
| `azure_storage_token` / `bearer_token` / `token` | Bearer token. |
| `azure_storage_use_emulator` / `object_store_use_emulator` / `use_emulator` | Use object store with azurite storage emulator. |
| `azure_endpoint` / `endpoint` | Override the endpoint used to communicate with blob storage. |
| `azure_use_fabric_endpoint` / `use_fabric_endpoint` | Use object store with url scheme account.dfs.fabric.microsoft.com. |
| `azure_msi_endpoint` / `azure_identity_endpoint` / `identity_endpoint` / `msi_endpoint` | Endpoint to request a imds managed identity token. |
| `azure_object_id` / `object_id` | Object id for use with managed identity authentication. |
| `azure_msi_resource_id` / `msi_resource_id` | Msi resource id for use with managed identity authentication. |
| `azure_federated_token_file` / `federated_token_file` | File containing token for Azure AD workload identity federation. |
| `azure_use_azure_cli` / `use_azure_cli` | Use azure cli for acquiring access token. |
| `azure_disable_tagging` / `disable_tagging` | Disables tagging objects. This can be desirable if not supported by the backing store. |

## AliCloud Object Storage Service Configuration

OSS credentials can be set in the environment variables `OSS_ACCESS_KEY_ID`,
`OSS_ACCESS_KEY_SECRET`, `OSS_REGION`, and `OSS_SECURITY_TOKEN`. Alternatively, they can be
passed as parameters to the `storage_options` parameter:

```python
import lance
ds = lance.dataset(
    "oss://bucket/path",
    storage_options={
        "oss_region": "oss-region",
        "oss_endpoint": "oss-endpoint",
        "oss_access_key_id": "my-access-key",
        "oss_secret_access_key": "my-secret-key",
        "oss_security_token": "my-session-token",
    }
)
```

| Key | Description |
|-----|-------------|
| `oss_endpoint` | OSS endpoint. Required (for example, `https://oss-cn-hangzhou.aliyuncs.com`). |
| `oss_access_key_id` | Access key ID used for OSS authentication. Optional if credentials are provided by environment. |
| `oss_secret_access_key` | Access key secret used for OSS authentication. Optional if credentials are provided by environment. |
| `oss_region` | OSS region (for example, `cn-hangzhou`). Optional. |
| `oss_security_token` | Security token for temporary credentials (STS). Optional. |

## Volcengine TOS Configuration

TOS credentials can be set in the environment variables `TOS_ACCESS_KEY_ID`,
`TOS_SECRET_ACCESS_KEY`, `TOS_ENDPOINT`, `TOS_REGION`, and `TOS_SECURITY_TOKEN`.
Lance also accepts the corresponding `VOLCENGINE_` environment variable prefix.
Alternatively, credentials can be passed as parameters to the `storage_options`
parameter; explicit `storage_options` override environment variables:

```python
import lance
ds = lance.dataset(
    "tos://bucket/path",
    storage_options={
        "tos_endpoint": "https://tos-cn-beijing.volces.com",
        "tos_region": "cn-beijing",
        "tos_access_key_id": "my-access-key",
        "tos_secret_access_key": "my-secret-key",
        "tos_security_token": "my-session-token",
    }
)
```

| Key | Description |
|-----|-------------|
| `tos_endpoint` | TOS endpoint. Required (for example, `https://tos-cn-beijing.volces.com`). |
| `tos_region` | TOS signing region (for example, `cn-beijing`). Optional. |
| `tos_access_key_id` | Access key ID used for TOS authentication. Optional if credentials are provided by environment. |
| `tos_secret_access_key` | Secret access key used for TOS authentication. Optional if credentials are provided by environment. |
| `tos_security_token` | Security token for temporary credentials. Optional. |

## Tencent Cloud COS Configuration

[COS (Cloud Object Storage)](https://cloud.tencent.com/product/cos) credentials can be set in environment variables prefixed
with `COS_` or `TENCENTCLOUD_` (for example, `COS_ENDPOINT`, `COS_SECRET_ID`,
`COS_SECRET_KEY`, `TENCENTCLOUD_REGION`, `TENCENTCLOUD_SECURITY_TOKEN`).
Alternatively, credentials can be passed as parameters to the `storage_options`
parameter; explicit `storage_options` override environment variables:

=== "Python"

    ```python
    import lance
    ds = lance.dataset(
        "cos://bucket/path",
        storage_options={
            "cos_endpoint": "https://cos.ap-guangzhou.myqcloud.com",
            "cos_secret_id": "my-secret-id",
            "cos_secret_key": "my-secret-key",
        }
    )
    ```

=== "Rust"

    In this Lance distribution, `tencent` is already part of the **default
    features** of the `lance` crate, so simply depending on `lance` is enough:

    ```toml
    [dependencies]
    lance = "*"
    ```

    You only need to enable the `tencent` feature explicitly in the following
    cases:

    - You opted out of default features, e.g.
      `lance = { version = "*", default-features = false, features = ["tencent", ...] }`.
    - You depend on `lance-io` directly (without `lance`); `tencent` is **not**
      a default feature of `lance-io`:
      `lance-io = { version = "*", features = ["tencent"] }`.

| Key | Description |
|-----|-------------|
| `cos_endpoint` | COS endpoint. Required (for example, `https://cos.ap-guangzhou.myqcloud.com`). Can also be set via the `COS_ENDPOINT` environment variable. |
| `cos_secret_id` | Secret ID used for COS authentication. Optional if credentials are provided by environment. |
| `cos_secret_key` | Secret key used for COS authentication. Optional if credentials are provided by environment. |
| `cos_enable_versioning` | Whether to enable object versioning on the bucket. Optional. |

!!! warning

    Tencent COS does not reliably enforce put-if-not-exists on buckets that have
    ever had versioning enabled, even if versioning is now suspended. To prevent
    silent manifest overwrites, Lance requires a custom distributed commit lock
    for COS writes. Pass the same `commit_lock` implementation to every Python
    writer, or provide a custom `CommitHandler` in Rust. Reads do not require a
    commit lock.

!!! note

    The OpenDAL `CosConfig` currently exposes a limited set of options. Additional
    settings such as the security token (`TENCENTCLOUD_SECURITY_TOKEN`) and region
    (`TENCENTCLOUD_REGION`) must be configured via environment variables.

## Hugging Face Configuration

Use `hf://datasets/<owner>/<repo>/<path>` to read a Lance dataset hosted on
Hugging Face. Pass these options through `storage_options`:

| Key | Description |
| --- | --- |
| `hf_token` | Hugging Face access token. Falls back to `HF_TOKEN` or `HUGGINGFACE_TOKEN` when omitted. |
| `hf_revision` | Repository revision, such as a commit ID, branch, or tag. Defaults to `main`. |
| `hf_download_mode` | `http` (default) or `xet`. |
| `hf_enable_resolve_cache` | `"true"` reuses resolved HTTP download URLs and XET file metadata across readers. Defaults to `"false"`. |

These options also accept names without the `hf_` prefix. The prefixed name
takes precedence when both are supplied. `hf_enable_resolve_cache` accepts only
the strings `"true"` and `"false"`.

Enable the resolve cache only when existing files will not change. Updates,
including changes behind a moving branch or tag, may remain invisible while
cached results are reused. HTTP download URLs refresh near expiry, and issued
URLs may remain usable until expiry after Hub permissions change. The cache
reduces Hub resolution requests; it does not cache file contents.

```python
import lance

ds = lance.dataset(
    "hf://datasets/owner/repo/data.lance",
    storage_options={"hf_enable_resolve_cache": "true"},
)
```

## GooseFS Configuration

[GooseFS](https://cloud.tencent.com/product/goosefs) is a distributed caching
filesystem. Lance accesses GooseFS through its Master gRPC service. The URL
format is either:

- `goosefs://host:port/path`, where `host:port` is the GooseFS Master address
  (default port: `9200`, may be omitted, e.g. `goosefs://10.0.0.1/path`)
- `goosefs:///path`, with an empty authority, when the master address comes
  from `goosefs_master_addr`, `GOOSEFS_MASTER_ADDR`, or
  `goosefs-site.properties`

`/path` is the filesystem path within GooseFS.

Manifest commits on `goosefs://` use `ConditionalPutCommitHandler`
(`PutMode::Create` / if-not-exists), backed by GooseFS master's atomic
no-replace rename so concurrent writers cannot clobber each other's
versioned manifests.

!!! warning "Mixed-version writers are NOT safe"

    The `if-not-exists` guarantee only holds when **every** writer for a
    dataset routes through this new handler. A writer running an older
    Lance release still selects `UnsafeCommitHandler` for `goosefs://`
    and writes the version path unconditionally, which can overwrite a
    manifest that an upgraded writer has already won. Safe concurrent
    commits therefore require:

    - all writers for the dataset run a Lance release that includes this
      routing change, **or**
    - writers share an external coordination boundary (e.g. a single-writer
      queue, table-level lock, or a gateway that serializes commits) that
      prevents the old code path from racing the new one.

    When upgrading in place, quiesce all writers (drain jobs, scale
    clients to zero, or route traffic through a writer coordinator) before
    rolling out the new Lance version, then bring writers back on the new
    version together.

!!! note "About the dataset path"

    `/path` is just an arbitrary directory inside GooseFS — Lance does **not**
    require the path to end with a `.lance` suffix. Any valid GooseFS directory
    works, for example:

    - `goosefs://10.0.0.1:9200/data/my-dataset`
    - `goosefs://10.0.0.1:9200/data/my-dataset.lance`
    - `goosefs://10.0.0.1:9200/lance-test/lance-io`

    The `.lance` suffix used in the examples below is only a naming convention
    that makes it easy to recognize a Lance dataset directory at a glance; it
    has no special meaning to Lance itself. The only requirement is that the
    same path is used consistently for reads and writes of a given dataset.

=== "Python"

    ```python
    import lance

    ds = lance.dataset(
        "goosefs://10.0.0.1:9200/data/my-dataset.lance",
        storage_options={
            "goosefs_auth_type": "simple",
            "goosefs_auth_username": "lance",
        },
    )
    ```

=== "Rust"

    In this Lance distribution, `goosefs` is already part of the **default
    features** of the `lance` crate, so simply depending on `lance` is enough:

    ```toml
    [dependencies]
    lance = "*"
    ```

    You only need to enable the `goosefs` feature explicitly in the following
    cases:

    - You opted out of default features, e.g.
      `lance = { version = "*", default-features = false, features = ["goosefs", ...] }`.
    - You depend on `lance-io` directly (without `lance`); `goosefs` is **not**
      a default feature of `lance-io`:
      `lance-io = { version = "*", features = ["goosefs"] }`.

    Open the underlying `lance_io::object_store::ObjectStore` directly (mirrors
    the integration test in `rust/lance-io/tests/goosefs_integration.rs`):

    ```rust
    use lance_io::object_store::ObjectStore;

    let uri = "goosefs://10.0.0.1:9200/lance-test/lance-io";
    let (store, path) = ObjectStore::from_uri(uri).await?;

    // Read / write through the underlying `object_store::ObjectStore` API
    store.inner.put(&path, (&b"hello"[..]).into()).await?;
    let result = store.inner.get(&path).await?;
    let bytes = result.bytes().await?;
    ```

    Open a Lance dataset with custom storage options:

    ```rust
    use std::collections::HashMap;
    use lance::dataset::DatasetBuilder;

    let mut storage_options = HashMap::new();
    storage_options.insert("goosefs_master_addr".to_string(), "10.0.0.1:9200".to_string());
    storage_options.insert("goosefs_auth_type".to_string(), "simple".to_string());
    storage_options.insert("goosefs_auth_username".to_string(), "lance".to_string());

    let dataset = DatasetBuilder::from_uri("goosefs://10.0.0.1:9200/data/my-dataset.lance")
        .with_storage_options(storage_options)
        .load()
        .await?;
    ```

=== "Java"

    Pass the GooseFS configuration through `ReadOptions.setStorageOptions`
    when opening the dataset:

    ```java
    import org.lance.Dataset;
    import org.lance.ReadOptions;

    import java.util.HashMap;
    import java.util.Map;

    Map<String, String> storageOptions = new HashMap<>();
    storageOptions.put("goosefs_master_addr", "10.0.0.1:9200");
    storageOptions.put("goosefs_auth_type", "simple");
    storageOptions.put("goosefs_auth_username", "lance");

    ReadOptions options = new ReadOptions.Builder()
        .setStorageOptions(storageOptions)
        .build();

    try (Dataset dataset = Dataset.open()
            .uri("goosefs://10.0.0.1:9200/data/my-dataset.lance")
            .readOptions(options)
            .build()) {
        // ... use the dataset
    }
    ```

    For writes, the same `storageOptions(...)` setter is available on
    `WriteDatasetBuilder` and `WriteFragmentBuilder`.

The Master address is resolved at OpenDAL build time (highest priority first):

1. The `GOOSEFS_MASTER_ADDR` environment variable.
2. `goosefs.master.rpc.addresses` or `goosefs.master.hostname` in
   `goosefs-site.properties` (discovered via `$GOOSEFS_CONFIG_FILE`,
   `$GOOSEFS_CONF_DIR`, `$GOOSEFS_HOME/conf`, `~/.goosefs`, and
   `/etc/goosefs`). This is the same file the GooseFS SDK already loads;
   a Hadoop-style `goosefs:///path` URL relies on it.
3. The `goosefs_master_addr` storage option (supports HA:
   `"addr1:port,addr2:port"`).
4. The host and port from the URL authority.

Lance forwards `goosefs_master_addr` and the URL authority as OpenDAL's
`master_addr`. It does **not** require a host in the URL, so
`goosefs:///path` can rely on `goosefs-site.properties`. OpenDAL fails the
store build only when none of the sources above supply an address.

A site file that declares masters outranks the URL authority because the file
can carry a full HA master list, which a single URI host cannot express.

`storage_options` keys **must be lowercase**. Uppercase or mixed-case spellings
such as `GOOSEFS_MASTER_ADDR` are rejected with an explicit error — they are
not ignored, and they are not treated as the matching environment variable.
Environment variables keep the `GOOSEFS_*` form.

| storage_options key | env var | Description |
|---------------------|---------|-------------|
| `goosefs_master_addr` | `GOOSEFS_MASTER_ADDR` | GooseFS Master address. Supports a single address (`host:port`) or comma-separated HA addresses (`addr1:port,addr2:port`). Optional if the address is provided in the URL, `GOOSEFS_MASTER_ADDR`, or `goosefs-site.properties`. |
| `goosefs_write_type` | `GOOSEFS_WRITE_TYPE` | Write type, e.g. `MUST_CACHE`, `CACHE_THROUGH`, `THROUGH`, `ASYNC_THROUGH`. Optional. |
| `goosefs_block_size` | `GOOSEFS_BLOCK_SIZE` | GooseFS block size (this is the GooseFS-side block size, not Lance's I/O block size). Accepts a raw byte count or GooseFS suffixes such as `64MB` (binary units: `1KB = 1024`). Optional. |
| `goosefs_chunk_size` | `GOOSEFS_CHUNK_SIZE` | Chunk size used when reading or writing files. Accepts a raw byte count or GooseFS suffixes such as `4MB` (binary units: `1KB = 1024`). Optional. |
| `goosefs_auth_type` | `GOOSEFS_AUTH_TYPE` | Authentication type. Either `nosasl` or `simple` (case-insensitive; the value is passed through to OpenDAL). Optional. |
| `goosefs_auth_username` | `GOOSEFS_AUTH_USERNAME` | Username used in `simple` authentication mode. Optional. |

!!! note "Running the GooseFS integration tests"

    The Rust integration tests for GooseFS live at
    `rust/lance-io/tests/goosefs_integration.rs` and are gated behind feature
    flags. They require a reachable GooseFS cluster (configured via the
    `GOOSEFS_MASTER_ADDR` and `GOOSEFS_AUTH_TYPE` environment variables) and
    can be run with:

    ```bash
    cargo test -p lance-io --features "goosefs goosefs-test" \
        --test goosefs_integration -- --ignored --nocapture --test-threads=1
    ```


================================================
FILE: docs/src/guide/observability.md
================================================
# Observability

Lance can publish operational metrics to your monitoring stack. The table below
is the authoritative catalogue of the metrics Lance emits, shared verbatim with
the Rust [`lance::metrics`](https://github.com/lance-format/lance/blob/main/rust/lance/src/metrics.md)
module documentation.

--8<-- "rust/lance/src/metrics.md"

## Collecting metrics

Lance emits through the [`metrics`](https://docs.rs/metrics) crate facade, so it
is not tied to a specific backend — you install a recorder/exporter and route
the metrics wherever you like. Metrics are available from the Rust, Python, and
Java APIs.

### Rust

Enable the `metrics` feature on the `lance` crate:

```toml
lance = { version = "...", features = ["metrics"] }
```

Then install any `metrics`-compatible recorder once at startup, before opening
datasets. For example, with
[`metrics-exporter-prometheus`](https://docs.rs/metrics-exporter-prometheus):

```rust
metrics_exporter_prometheus::PrometheusBuilder::new()
    .install()
    .expect("install Prometheus recorder");
```

Any recorder works — Prometheus, StatsD, an OpenTelemetry bridge, and so on.
When no recorder is installed, emission is a cheap no-op.

### Python

Unlike Rust, the Python bindings do not let you plug in an arbitrary recorder:
bridging one across the FFI boundary into the Rust `metrics` facade would be
complicated and inefficient. Instead `pylance` standardizes on OpenTelemetry,
which has good Python support, as its recorder.

The `pylance` wheels are built with the `metrics` feature enabled. Install the
OpenTelemetry extra and call `instrument_lance_metrics`, which registers Lance's
metrics as observable instruments on your OpenTelemetry `MeterProvider`:

```bash
pip install "pylance[otel]"
```

```python
from lance.otel import instrument_lance_metrics

# Uses the global MeterProvider; pass meter_provider=... to target a specific one.
instrument_lance_metrics()
```

### Java

The Java SDK includes an OpenTelemetry bridge in `org.lance.otel`. Register it
before opening datasets or performing Lance IO so the process-global Rust
recorder sees every emitted metric:

```java
import org.lance.otel.LanceMetrics;

LanceMetrics.instrument();
```

The OpenTelemetry API is a dependency of the Java SDK. The application must
still configure an OpenTelemetry SDK, metric reader, and exporter for collection
and delivery.

The no-argument method uses OpenTelemetry's global `MeterProvider`. To register
with an explicitly configured provider, pass it directly:

```java
SdkMeterProvider provider = SdkMeterProvider.builder()
    .registerMetricReader(metricReader)
    .build();
LanceMetrics.instrument(provider);
```

Repeated calls with the same provider are idempotent. Passing a different
provider unregisters the existing callback before registering the new one. Call
`LanceMetrics.close()` to stop exporting while retaining the process-global Rust
metric state.

From there the metrics flow through whatever OpenTelemetry pipeline you have
configured (OTLP, Prometheus, console, …). Because OpenTelemetry has no
asynchronous histogram instrument, histograms are exported Prometheus-style as
three observable counters: `<name>_bucket`, `<name>_count`, and `<name>_sum`.
Each `<name>_bucket` sample carries an `le` ("less than or equal") attribute
giving that bucket's inclusive upper bound in the metric's unit; the bucket
count is cumulative, covering every observation at or below `le`. For example, a
`lance_object_store_request_duration_seconds_bucket` sample with `le="0.5"`
counts all requests that completed in 0.5 seconds or less, while `le="+Inf"` is
the total count.


================================================
FILE: docs/src/guide/performance.md
================================================
# Lance Performance Guide

This guide provides tips and tricks for optimizing the performance of your Lance applications.

## Logging

Lance uses the `log` crate to log messages. Displaying these log messages will depend on the client
library you are using. For rust, you will need to configure a logging subscriber. For more details
ses the [log](https://docs.rs/log/latest/log/) docs. The Python and Java clients configure a default
logging subscriber that logs to stderr.

The Python/Java logger can be configured with several environment variables:

- `LANCE_LOG`: Controls log filtering based on log level and target. See the [env_logger](https://docs.rs/env_logger/latest/env_logger/) docs for more details. The `LANCE_LOG` environment variable replaces the `RUST_LOG` environment variable.
- `LANCE_TRACING`: Controls tracing filtering based on log level. Key tracing events described below are emitted at
  the `info` level. However, additional spans and events are available at the `debug` level which may be useful for
  debugging performance issues. The default tracing level is `info`.
- `LANCE_LOG_STYLE`: Controls whether colors are used in the log messages. Valid values are `auto`, `always`, `never`.
- `LANCE_LOG_TS_PRECISION`: The precision of the timestamp in the log messages. Valid values are `ns`, `us`, `ms`, `s`.
- `LANCE_LOG_FILE`: Redirects Rust log messages to the specified file path instead of stderr. When set, Lance will create the file and any necessary parent directories. If the file cannot be created (e.g., due to permission issues), Lance will fall back to logging to stderr.

## Trace Events

Lance uses tracing to log events. If you are running `pylance` then these events will be emitted
as log messages. For Rust connections you can use the `tracing` crate to capture these events.

Rust tracing targets are listed below. In `pylance` logs, trace events are emitted under a
`lance::events::` prefix so they can be filtered separately from normal log records. For example,
`LANCE_LOG="warn,lance::events::object_store::throttle=info"` shows storage throttling events
without enabling other Lance event logs.

### File Audit

File audit events are emitted when significant files are created or deleted.

| Event               | Parameter | Description                                                                |
| ------------------- | --------- | -------------------------------------------------------------------------- |
| `lance::file_audit` | `mode`    | The mode of I/O operation (create, delete, delete_unverified)              |
| `lance::file_audit` | `type`    | The type of file affected (manifest, data file, index file, deletion file) |

### Dataset Events

Dataset events are emitted when datasets are loaded, written, committed, deleted, compacted, or cleaned.

| Event                   | Parameter   | Description                                                               |
| ----------------------- | ----------- | ------------------------------------------------------------------------- |
| `lance::dataset_events` | `event`     | The dataset event type (loading, writing, committed, deleting, and others) |
| `lance::dataset_events` | `uri`       | The dataset URI                                                           |
| `lance::dataset_events` | `mode`      | The write mode                                                            |
| `lance::dataset_events` | `operation` | The committed transaction operation                                       |
| `lance::dataset_events` | `predicate` | The delete predicate                                                      |
| `lance::dataset_events` | `columns`   | The removed columns                                                       |

### Object Store Throttle Events

Object store throttle events are emitted when Lance observes cloud storage throttle responses and
reduces or retries request rates.

| Event                            | Parameter       | Description                              |
| -------------------------------- | --------------- | ---------------------------------------- |
| `lance::object_store::throttle`  | `previous_rate` | The request rate before AIMD adjustment  |
| `lance::object_store::throttle`  | `new_rate`      | The request rate after AIMD adjustment   |
| `lance::object_store::throttle`  | `attempt`       | The retry attempt for retry debug events |
| `lance::object_store::throttle`  | `error`         | The underlying object store throttle error      |

### I/O Events

I/O events are emitted when significant I/O operations are performed, particularly
those related to indices. These events are NOT emitted when the index is loaded from
the in-memory cache. Correct cache utilization is important for performance and these
events are intended to help you debug cache usage.

| Event              | Parameter | Description                                                                                          |
| ------------------ | --------- | ---------------------------------------------------------------------------------------------------- |
| `lance::io_events` | `type`    | The type of I/O operation (open_scalar_index, open_vector_index, load_vector_part, load_scalar_part) |

### Execution Events

Execution events are emitted when an execution plan is run. These events are useful for
debugging query performance.

| Event              | Parameter           | Description                                                    |
| ------------------ | ------------------- | -------------------------------------------------------------- |
| `lance::execution` | `type`              | The type of execution event (plan_run is the only type today)  |
| `lance::execution` | `output_rows`       | The number of rows in the output of the plan                   |
| `lance::execution` | `iops`              | The number of I/O operations performed by the plan             |
| `lance::execution` | `bytes_read`        | The number of bytes read by the plan                           |
| `lance::execution` | `indices_loaded`    | The number of indices loaded by the plan                       |
| `lance::execution` | `parts_loaded`      | The number of index partitions loaded by the plan              |
| `lance::execution` | `index_comparisons` | The number of comparisons performed inside the various indices |

## Threading Model

Lance is designed to be thread-safe and performant. Lance APIs can be called concurrently unless
explicitly stated otherwise. Users may create multiple tables and share tables between threads.
Operations may run in parallel on the same table, but some operations may lead to conflicts. For
details see [conflict resolution](../format/table/transaction.md/#conflict-resolution).

Most Lance operations will use multiple threads to perform work in parallel. There are two thread
pools in lance: the IO thread pool and the compute thread pool. The IO thread pool is used for
reading and writing data from disk. The compute thread pool is used for performing computations
on data. The number of threads in each pool can be configured by the user.

The IO thread pool is used for reading and writing data from disk. The number of threads in the IO
thread pool is determined by the object store that the operation is working with. Local object stores
will use 8 threads by default. Cloud object stores will use 64 threads by default. This is a fairly
conservative default and you may need 128 or 256 threads to saturate network bandwidth on some cloud
providers. The `LANCE_IO_THREADS` environment variable can be used to override the number of IO
threads. If you increase this variable you may also want to increase the `io_buffer_size`.

The compute thread pool is used for performing computations on data. The number of threads in the
compute thread pool is determined by the number of cores on the machine. The number of threads in
the compute thread pool can be overridden by setting the `LANCE_CPU_THREADS` environment variable.
This is commonly done when running multiple Lance processes on the same machine (e.g when working with
tools like Ray). Keep in mind that decoding data is a compute intensive operation, even if a workload
seems I/O bound (like scanning a table) it may still need quite a few compute threads to achieve peak
performance.

## Memory Requirements

Lance is designed to be memory efficient. Operations should stream data from disk and not require
loading the entire dataset into memory. However, there are a few components of Lance that can use
a lot of memory.

### Metadata Cache

Lance uses a metadata cache to speed up operations. This cache holds various pieces of metadata such
as file metadata, dataset manifests, etc. This cache is an LRU cache that is sized by bytes. The default
size is 1 GiB.

The metadata cache is not shared between tables by default. For best performance you should create a
single table and share it across your application. Alternatively, you can create a single session
and specify it when you open tables.

Keys are often a composite of multiple fields and all keys are scoped to the dataset URI. The following items are stored in the metadata cache:

| Item              | Key                                              | What is stored                      |
| ----------------- | ------------------------------------------------ | ----------------------------------- |
| Dataset Manifests | Dataset URI, version, and etag                   | The manifest for the dataset        |
| Transactions      | Dataset URI, version                             | The transaction for the dataset     |
| Deletion Files    | Dataset URI, fragment_id, version, id, file_type | The deletion vector for a frag      |
| Row Id Mask       | Dataset URI, version                             | The row id sequence for the dataset |
| Row Id Index      | Dataset URI, version                             | The row id index for the dataset    |
| Row Id Sequence   | Dataset URI, fragment_id, row_id_meta            | The row id sequence for a fragment  |
| Index Metadata    | Dataset URI, version                             | The index metadata for the dataset  |
| Index Details¹    | Dataset URI, index uuid                          | The index details for an index      |
| File Global Meta  | Dataset URI, file path                           | The global metadata for a file      |
| File Column Meta  | Dataset URI, file path, column index             | The search cache for a column       |

Notes:

1. This is only stored for very old indexes which don't store their details in the manifest.

### Index Cache

Lance uses an index cache to speed up queries. This caches vector and scalar indices in memory. The
max size of this cache can be configured when creating a `LanceDataset` using the `index_cache_size_bytes`
parameter. This cache is an LRU cached that is sized by bytes. The default size is 6 GiB.
You can view the size of this cache by inspecting the result of `dataset.session().size_bytes()`.

The index cache is not shared between tables. For best performance you should create a single table and
share it across your application.

**Note**: `index_cache_size` (specified in entries) was deprecated since version 0.30.0. Use
`index_cache_size_bytes` (specified in bytes) for new code.

### Scanning Data

Searches (e.g. vector search, full text search) do not use a lot of memory to hold data because they don't
typically return a lot of data. However, scanning data can use a lot of memory. Scanning is a streaming
operation but we need enough memory to hold the data that we are scanning. The amount of memory needed is
largely determined by the `io_buffer_size` and the `batch_size` variables.

Each I/O thread should have enough memory to buffer an entire page of data. Pages today are typically between
8 and 32 MB. This means, as a rule of thumb, you should generally have about 32MB of memory per I/O thread.
The default `io_buffer_size` is 2GB which is enough to buffer 64 pages of data. If you increase the number
of I/O threads you should also increase the `io_buffer_size`.

Scans will also decode data (and run any filtering or compute) in parallel on CPU threads. The amount of data
decoded at any one time is determined by the `batch_size` and the size of your rows. Each CPU thread will
need enough memory to hold one batch. Once batches are delivered to your application, they are no longer tracked
by Lance and so if memory is a concern then you should also be careful not to accumulate memory in your own
application (e.g. by running `to_table` or otherwise collecting all batches in memory.)

The default `batch_size` is 8192 rows. When you are working with mostly scalar data you want to keep batches
around 1MB and so the amount of memory needed by the compute threads is fairly small. However, when working with
large data you may need to turn down the `batch_size` to keep memory usage under control. For example, when
working with 1024-dimensional vector embeddings (e.g. 32-bit floats) then 8192 rows would be 32MB of data. If you
spread that across 16 CPU threads then you would need 512MB of compute memory per scan. You might find working
with 1024 rows per batch is more appropriate.

#### Tuning remote scans

An ordered dataset scan still overlaps I/O from multiple fragments. `scan_in_order=True` controls the order in
which batches are returned; it does not make fragment reads sequential. This is why a dataset scan can issue
more concurrent requests than scanning one fragment directly. The following controls tune different parts of
the scan:

* `fragment_readahead` limits how many fragments may have reads scheduled concurrently. Set it to `1` to match
  the fragment-level I/O pattern, then increase it if the storage connection has spare bandwidth.
* `LANCE_IO_THREADS` limits concurrent storage requests for the process. Cloud stores default to 64, which is
  intended for high-bandwidth, in-region access and can be too aggressive across regions or over the public
  internet.
* `io_buffer_size` limits buffered I/O bytes and applies backpressure when decoding falls behind.
* `batch_readahead` limits concurrent batch decoding. It does not control the size of storage range requests.

For a bandwidth-constrained remote connection, start with conservative settings and tune upward:

```shell
LANCE_IO_THREADS=8 python scan.py
```

```python
scanner = dataset.scanner(
    fragment_readahead=1,
    batch_readahead=2,
    io_buffer_size=64 * 1024 * 1024,
)
for batch in scanner.to_batches():
    process(batch)
```

Lance reads encoded pages from storage, so reducing `batch_size` changes the returned and decoded batch sizes
but may not reduce the initial range request. The first batch can require loading one encoded page for each
selected column.

In summary, scans could use up to `(2 * io_buffer_size) + (batch_size * num_compute_threads)` bytes of memory.
Keep in mind that `io_buffer_size` is a soft limit (e.g. we cannot read less than one page at a time right now)
and so it is not necessarily a bug if you see memory usage exceed this limit by a small margin.

### Cloud Store Throttling

Cloud object stores (S3, GCS, Azure) are automatically wrapped with an AIMD (Additive Increase / Multiplicative
Decrease) rate limiter. When the store returns throttle errors (HTTP 429/503), the request rate decreases
multiplicatively. During sustained success, the rate increases additively. This applies to all operations
(reads, writes, deletes, lists) and replaces the old `LANCE_PROCESS_IO_THREADS_LIMIT` process-wide cap.

Local and in-memory stores are **not** throttled.

The AIMD throttle can be tuned via storage options or environment variables. Storage options take precedence
over environment variables:

| Setting            | Storage Option Key              | Env Var                         | Default |
| ------------------ | ------------------------------- | ------------------------------- | ------- |
| Initial rate       | `lance_aimd_initial_rate`       | `LANCE_AIMD_INITIAL_RATE`       | 2000    |
| Min rate           | `lance_aimd_min_rate`           | `LANCE_AIMD_MIN_RATE`           | 1       |
| Max rate           | `lance_aimd_max_rate`           | `LANCE_AIMD_MAX_RATE`           | 5000    |
| Decrease factor    | `lance_aimd_decrease_factor`    | `LANCE_AIMD_DECREASE_FACTOR`    | 0.5     |
| Additive increment | `lance_aimd_additive_increment` | `LANCE_AIMD_ADDITIVE_INCREMENT` | 300     |
| Burst capacity     | `lance_aimd_burst_capacity`     | `LANCE_AIMD_BURST_CAPACITY`     | 100     |

These initial settings are balanced and should work for most
use cases. For example, S3 can typically get up to 5000
req/s and with these settings we should get there in about
10 seconds.

## Fragment Sizing

A Lance table is a collection of fragments tracked by a manifest. How you size those fragments
trades off two classes of work:

- **Manifest-level operations** scale with the *number* of fragments. Every dataset mutation
  (appends, metadata updates, schema changes, compactions, etc.) rewrites the manifest, so a
  larger fragment list makes every write slower. Reads pay a similar cost up front: opening a
  dataset, listing fragments, planning a scan, and resolving transaction conflicts at the
  dataset level all walk the manifest.
- **Fragment-level operations** scale with the *size* of a fragment. These include scans
  against a matching fragment, compaction, updates, deletes, and `merge_insert`. Conflict
  detection for these operations is also done at the fragment level.

Fewer, larger fragments make manifest-level operations cheap but make each fragment-level
operation heavier and increase the chance of conflicts when many writers target the same
fragment. More, smaller fragments do the reverse.

Practical guidance:

- The default of 1M rows per fragment works well up to ~1B rows. Past that, bumping toward
  ~100M rows per fragment is reasonable, though fragment-count limits are rarely the bottleneck
  in practice.
- Tens of thousands of fragments per table is generally fine.
- Keep individual fragments well under object-store object-size limits (S3 caps at 5 TB, and
  stores tend to misbehave well before that). 10 GB–100 GB per fragment is a reasonable upper
  range; 1 TB is a hard ceiling.
- If you run many concurrent updates, deletes, or `merge_insert` operations, err toward more
  fragments — conflict detection is per-fragment, so too few fragments leads to excess
  retries.

## Conflict Handling

Lance supports concurrent operations on the same table using optimistic concurrency control. When two
operations conflict, one of them must be retried. Retries are handled automatically but they repeat
work that has already been done, which can hurt throughput. Understanding and minimizing conflicts is
important for maintaining good performance in write-heavy workloads.

Common sources of conflicts include:

- Concurrent compaction and index building, since both need to modify the same indices
- Update operations that affect the same fragments, since both need to rewrite the same data files

For more details on which operations conflict with each other, see
[conflict resolution](../format/table/transaction.md#conflict-resolution).

### Fragment Reuse Index

Compaction is one of the most expensive write operations because it rewrites data files and, by
default, remaps all indices to reflect the new row addresses. When compaction and index building
run concurrently, they often conflict because both need to modify the same indices. This typically
causes the compaction to fail and retry, and repeated failures can cause table layout to degrade
over time.

The Fragment Reuse Index (FRI) solves this by allowing compaction to skip the index remap step.
Instead of immediately updating indices, compaction records a mapping from old fragment row
addresses to new ones. When indices are loaded into the cache, the FRI is applied to translate
the old row addresses to the current ones. This adds a small cost to index load time but does
not affect query performance once the index is cached.

This decoupling means compaction and index building no longer conflict, which is especially
valuable for tables that are continuously ingesting data while also maintaining indices.

To enable the FRI, set `defer_index_remap=True` when compacting:

```python
dataset.optimize.compact_files(defer_index_remap=True)
```

Rust callers can open the FRI for the dataset version they have loaded with
`Dataset::frag_reuse_index()`, which returns `None` when that version has no FRI. The returned
index exposes the raw remap: a physical row address is either unmapped, deleted by a recorded
compaction, or mapped to the last address reached through the retained mappings. Neither outcome
is validated against the loaded manifest. Unmapped addresses may still have moved in a compaction
whose history was trimmed, and mapped destinations may since have been removed, so callers that
need a complete translation must verify coverage and destinations themselves.

For details on the index format and usage patterns, see the
[Fragment Reuse Index specification](../format/index/system/frag_reuse.md).

## Indexes

Training and searching indexes can have unique requirements for compute and memory. This section provides some
guidance on what can be expected for different index types.

### BTree Index

The BTree index is a two-level structure that provides efficient range queries and sorted access.
It strikes a balance between an expensive memory structure containing all values and an expensive disk
structure that can't be efficiently searched.

Training a BTree index is done by sorting the column. This is done using an [external sort](https://en.wikipedia.org/wiki/External_sorting) to constrain the total memory usage to a reasonable amount. Updating a BTree index does not
require re-sorting the entire column. The new values are sorted and the existing values are merged into the new sorted
values in linear time.

#### Storage Requirements

The BTree index is essentially a sorted copy of a column. The storage requirements are therefore the same as the column
but an additional 4 bytes per value is required to store the row ID and there is a small lookup structure which
should be roughly 0.001% of the size of the column.

#### Memory Requirements

Training a BTree index requires some RAM but the current implementation spills to disk rather aggressively and so the
total memory usage is fairly low.

When searching a BTree index, the index is loaded into the index cache in pages. Each page contains 4096 values.

#### Performance

The sort stage is the most expensive step in training a BTree index. The time complexity is O(n log n) where n is the number of rows in the column. At very large scales this can be a bottleneck and a distributed sort may be necessary. Lance currently does
not have anything builtin for this but work is underway to add this functionality. Training an index in parts as the data grows
may be slightly more efficient than training the entire index at once if you have the flexibility to do so.

When the BTree index is fully loaded into the index cache, the search time scales linearly with the number of rows that match the
query. When the BTree index is not fully loaded into the index cache, the search time will be controlled by the number of pages
that need to be loaded from disk and the speed of storage. The parts_loaded metric in the execution metrics can tell you how many
pages were loaded from disk to satisfy a query.

### Bitmap Index

The Bitmap index is an inverted lookup table that stores a bitmap for each possible value in the column. These bitmaps are compressed and serialized as a [Roaring Bitmap](https://roaringbitmap.org/).

A bitmap index is currently trained by accumulating the column into a hash map from value to a vector of row ids. Each value
is then serialized into a bitmap and stored in a file.

### Storage Requirements

The size of a bitmap index is difficult to calculate precisely but will generally scale with the number of unique values in the
column since a unique bitmap is required for each value and a single bitmap with all rows will compress more efficiently than
many bitmaps with a small number of rows.

#### Memory Requirements

Since training a bitmap index requires collecting the values into a hash map you will need at least 8 bytes of memory per row.
In addition, if you have many unique values, then you will need additional memory for the keys of the hash map. Training large
bitmaps with many unique values at scale can be memory intensive.

When a bitmap index is searched, bitmaps are loaded into the session cache individually. The size of the bitmap will depend on
the number of rows that match the token.

### Performance

When the bitmap index is fully loaded into the index cache, the search time scales linearly with the number of values that the
query requires. This makes the bitmap very fast for equality queries or very small ranges. Queries against large ranges are
currently extremely slow and the btree index is much faster for large range queries.

When a bitmap index is not fully loaded into the index cache, the search time will be controlled by the number of bitmaps that
need to be loaded from disk and the speed of storage. The parts_loaded metric in the execution metrics can tell you how many
bitmaps were loaded from disk to satisfy a query.

### Vector Index

Vector indexes (IVF_PQ, IVF_HNSW_SQ, etc.) are built in multiple phases, each with different memory requirements.

#### IVF Training

The IVF (Inverted File) phase clusters vectors into partitions using KMeans. To train the KMeans model, a sample of the
dataset is loaded into memory. The size of this sample is determined by:

```
training_data = num_partitions * sample_rate * dimension * sizeof(data_type)
```

The default `sample_rate` is 256. For example, with 1024 partitions, 768-dimensional float32 vectors, and the default
sample rate:

```
1024 * 256 * 768 * 4 bytes = 768 MiB
```

In addition to the training data, each KMeans iteration allocates membership and distance vectors proportional to the
number of training vectors (8 bytes per vector). The centroids themselves require `num_partitions * dimension *
sizeof(data_type)` bytes. In practice, the training data dominates and these additional allocations are small in
comparison.

If the dataset has fewer rows than `num_partitions * sample_rate`, the entire dataset is used for training instead.

#### Quantizer Training

After IVF training, a quantizer (e.g. PQ, SQ) is trained to compress vectors. This phase may sample some of the
dataset, but the sample size is tied to properties of the quantizer and the vector dimension rather than the size of the
dataset. As a result, quantizer training typically requires very little RAM compared to the IVF phase.

#### Shuffling

The final phase scans the entire vector column, transforms each vector (assigning it to an IVF partition and quantizing
it), and writes the results into per-partition files on disk. This is a streaming operation — data is not accumulated in
memory.

The input scan uses a 2 GiB I/O readahead buffer by default (configurable via `LANCE_DEFAULT_IO_BUFFER_SIZE`) and reads
batches of 8,192 rows. Incoming batches are transformed in parallel, with `num_cpus - 2` batches in flight at a time
(configurable via `LANCE_CPU_THREADS`). Each batch is sorted by partition ID and the slices are written directly to the
corresponding partition file. The in-flight memory during this phase is roughly:

```
io_readahead_buffer + num_cpu_threads * batch_size * (raw_vector_size + transformed_vector_size)
```

Each partition has an open file writer with roughly 8 MiB of accumulation buffer. In practice there shouldn't be that
much data accumulated in a single partition anyways. Instead, the max accumulation will be roughly the final size of
the partitions which comes out to `num_rows * (num_sub_vectors + 8) bytes`. For example, 100M rows with a 1536-dimensional
vector will have 96 sub-vectors and so the max accumulation will be ~10GB. The additional 8 bytes per row is for the row ID.

#### Storage Requirements

The on-disk size of a vector index consists of the IVF centroids and the quantized vectors.

The centroids require:

```
num_partitions * dimension * sizeof(data_type)
```

This is typically small. For example, 10K partitions with 768-dimensional float32 vectors is only 30 MiB.

The quantized vectors make up the bulk of the index. Each row stores a quantized code plus an 8-byte row ID. The
exact size depends on the quantizer:

**PQ (Product Quantization):** Each sub-vector is quantized to a single byte, so each row requires
`num_sub_vectors + 8` bytes. For example, 100M rows with 96 sub-vectors:

```
100M * (96 + 8) = ~9.7 GiB
```

**SQ (Scalar Quantization):** Each dimension is independently quantized to a single byte, so each row requires
`dimension + 8` bytes. SQ preserves more information than PQ but requires more storage. For example, 100M rows with
768-dimensional vectors:

```
100M * (768 + 8) = ~72.3 GiB
```

**RQ (RaBitQ):** New indexes default to 5 bits per dimension. Every bit width stores a 1-bit sign
code plus three 4-byte correction factors. Multi-bit indexes also store the remaining bits in
64-dimension-padded blocks and two additional 4-byte correction factors. Including the 8-byte row
ID, the approximate size per row is:

- 1-bit: `dimension / 8 + 20` bytes
- Multi-bit: `dimension / 8 + round_up(dimension, 64) * (num_bits - 1) / 8 + 28` bytes

For example, the default 5-bit index for 100M rows with 768 dimensions requires:

```
100M * (768 / 8 + 768 * 4 / 8 + 28) = ~47.3 GiB
```

The 5-bit default retains more information for the higher-fidelity distance estimates used by
`Normal` and `Accurate` search modes, at the cost of more quantization work and index I/O during
the build and a larger index. `Fast` search mode uses only the 1-bit sign code even when the index
stores additional bits. Set `num_bits=1` explicitly to minimize index size and build I/O; the same
100M-row example uses about 10.8 GiB, but searches cannot use the multi-bit distance estimate and
may have lower recall.

#### AMX Acceleration

On Linux x86_64 with an AMX-FP16 CPU (Intel Granite Rapids / Xeon 6 and newer), a `float16`
vector column indexed with `dot` distance uses the AMX tile instructions, provided the build
machine had clang >= 16 or gcc >= 13 to compile the kernel. There is nothing to enable —
Lance checks the CPU at run time and falls back to the previous implementation everywhere else.

The accelerated paths are also shape-gated, because below these sizes a tile pass costs more
than it saves and the kernel declines the work:

| Condition | Why |
|---|---|
| `float16` vectors, `dot` distance | The kernel is fp16-specific; other types and metrics keep their existing paths |
| `dimension >= 32` | One tile pass covers 32 dimensions; a shorter vector would be all scalar cleanup |
| `num_centroids >= 32` | The GEMM steps its centroid loop by 32 and has no partial-tile path |

Anything outside them behaves exactly as it does today, so a small dataset or a low-dimensional
column simply keeps the previous implementation rather than changing behaviour.

Index build also changes algorithm where all of the above hold: comparing every vector against
every centroid becomes affordable, so partition assignment is exact instead of approximated with
a graph search over the centroids. Recall improves, and partition assignments differ from what an
older build produced.

Set `LANCE_DISABLE_AMX=1` to take the AMX paths out of service without rebuilding — for
A/B measurement, or to get the previous behaviour back. Because it also moves partition
assignment back to the approximate path, an index built with it set is not equivalent to one
built without it; compare recall, not just build time.


================================================
FILE: docs/src/guide/read_and_write.md
================================================
# Read and Write Data

## Writing Lance Dataset

If you're familiar with [Apache PyArrow](https://arrow.apache.org/docs/python/getstarted.html), you'll find that creating a Lance dataset is straightforward.
Begin by writing a `pyarrow.Table` using the `lance.write_dataset` function.

```python
import lance
import pyarrow as pa

table = pa.Table.from_pylist([{"name": "Alice", "age": 20},
                              {"name": "Bob", "age": 30}])
ds = lance.write_dataset(table, "./alice_and_bob.lance")
```

If the dataset is too large to fully load into memory, you can stream data using `lance.write_dataset`
also supports `Iterator` of `pyarrow.RecordBatch` es.
You will need to provide a `pyarrow.Schema` for the dataset in this case.

```python
from typing import Iterator

def producer() -> Iterator[pa.RecordBatch]:
    """An iterator of RecordBatches."""
    yield pa.RecordBatch.from_pylist([{"name": "Alice", "age": 20}])
    yield pa.RecordBatch.from_pylist([{"name": "Bob", "age": 30}])

schema = pa.schema([
    ("name", pa.string()),
    ("age", pa.int32()),
])

ds = lance.write_dataset(producer(),
                         "./alice_and_bob.lance",
                         schema=schema, mode="overwrite")
print(ds.count_rows())  # Output: 2
```

`lance.write_dataset` supports writing `pyarrow.Table`, `pandas.DataFrame`,
`pyarrow.dataset.Dataset`, and `Iterator[pyarrow.RecordBatch]`.

## Choosing a data file version

`data_storage_version` selects the format of newly written data files. For an
existing V2 dataset, an operation can select `"2.0"`, `"2.1"`, `"2.2"`, or `"2.3"`
without rewriting the other files. The dataset's `data_storage_version` property
is the default for writes that omit a target, not a summary of its existing
files. Create and overwrite establish this default; append, update, merge-insert,
and compaction do not change it. V1 and V2 cannot be mixed.

```python
import lance
import pyarrow as pa

data = pa.table({"id": [1, 2], "value": [10, 20]})
ds = lance.write_dataset(data, "./versions.lance", data_storage_version="2.1")
ds.update({"value": "value + 1"}, data_storage_version="2.2")
assert ds.data_storage_version == "2.1"

ds.merge_insert("id").when_not_matched_insert_all().data_storage_version("2.2").execute(
    pa.table({"id": [3], "value": [30]})
)
ds.optimize.compact_files(data_storage_version="2.2")
```

The `"stable"` and `"next"` selectors resolve according to the engine release.
Use exact versions when the output identity must be independent of that release.
V2.3 is currently unstable: files written by one unstable revision may not be
readable by a later revision. Use it only for experimentation.
Compaction plans fix the target before distributing tasks, including when the
target comes from the dataset default. The target survives Python pickle and
Java serialization; workers do not reinterpret it using their own release defaults.

Java update, merge-insert, and compaction options use
`withDataStorageVersion(DataStorageVersion.V2_2)`. The shared `DataStorageVersion`
enum also provides `STABLE` and `NEXT` selectors.

Compaction can convert selected fragments to another supported V2 version.
Binary copy requires every selected file to match the output version, as well
as the usual eligibility checks. In particular, overlays need reencoding to
preserve updated values. `try_binary_copy` falls back to reencoding when inputs
are ineligible; `force_binary_copy` rejects them. A version mismatch error includes
the target version, actual version, and file path. A persistent compaction target
can be set through `lance.compaction.data_storage_version` in the table config;
an explicit operation target takes precedence.

### Upgrading clients before mixed-version writes

Before writing files that differ from the dataset default, upgrade every reader
and writer to a mixed-version-aware release. Drain, restart, or fence writers
that opened the dataset using an older release. The commit automatically sets
the paired mixed-version reader/writer feature flags when needed; there is no
separate activation API. These flags remain set even if compaction later makes
the files homogeneous again.

Older clients that do not recognize the flags reject the resulting snapshots.
The flags cannot retroactively fence an older writer that already read a prior
manifest. Historical homogeneous datasets remain readable by clients that
support their file versions; see [table versioning](../format/table/versioning.md)
for the feature flag contract.

## Adding Rows

To insert data into your dataset, you can use either `LanceDataset.insert`
or `lance.write_dataset` with `mode=append`.

```python
import lance
import pyarrow as pa

table = pa.Table.from_pylist([{"name": "Alice", "age": 20},
                              {"name": "Bob", "age": 30}])
ds = lance.write_dataset(table, "./insert_example.lance")

new_table = pa.Table.from_pylist([{"name": "Carla", "age": 37}])
ds.insert(new_table)
print(ds.to_table().to_pandas())
#     name  age
# 0  Alice   20
# 1    Bob   30
# 2  Carla   37

new_table2 = pa.Table.from_pylist([{"name": "David", "age": 42}])
ds = lance.write_dataset(new_table2, ds, mode="append")
print(ds.to_table().to_pandas())
#     name  age
# 0  Alice   20
# 1    Bob   30
# 2  Carla   37
# 3  David   42
```

## Deleting rows

Lance supports deleting rows from a dataset using a SQL filter, as described in [Filter push-down](#filter-push-down).
For example, to delete Bob's row from the dataset above, one could use:

```python
import lance

dataset = lance.dataset("./alice_and_bob.lance")
dataset.delete("name = 'Bob'")
dataset2 = lance.dataset("./alice_and_bob.lance")
print(dataset2.to_table().to_pandas())
#     name  age
# 0  Alice   20
```

!!! note

    [Lance Format is immutable](../format/index.md). Each write operation creates a new version of the dataset,
    so users must reopen the dataset to see the changes. Likewise, rows are removed by marking
    them as deleted in a separate deletion index, rather than rewriting the files. This approach
    is faster and avoids invalidating any indices that reference the files, ensuring that subsequent
    queries do not return the deleted rows.

## Updating rows

Lance supports updating rows based on SQL expressions with the
`lance.LanceDataset.update` method. For example, if we notice
that Bob's name in our dataset has been sometimes written as `Blob`, we can fix
that with:

```python
import lance

dataset = lance.dataset("./alice_and_bob.lance")
dataset.update({"name": "'Bob'"}, where="name = 'Blob'")
```

The update values are SQL expressions, which is why `'Bob'` is wrapped in single
quotes. This means we can use complex expressions that reference existing columns if
we wish. For example, if two years have passed and we wish to update the ages
of Alice and Bob in the same example, we could write:

```python
import lance

dataset = lance.dataset("./alice_and_bob.lance")
dataset.update({"age": "age + 2"})
```

If you are trying to update a set of individual rows with new values then it is often
more efficient to use the merge insert operation described below.

```python
import lance

# Change the ages of both Alice and Bob
new_table = pa.Table.from_pylist([{"name": "Alice", "age": 30},
                                  {"name": "Bob", "age": 20}])

# This works, but is inefficient, see below for a better approach
dataset = lance.dataset("./alice_and_bob.lance")
for idx in range(new_table.num_rows):
  name = new_table[0][idx].as_py()
  new_age = new_table[1][idx].as_py()
  dataset.update({"age": new_age}, where=f"name='{name}'")
```

## Merge Insert

Lance supports a merge insert operation. This can be used to add new data in bulk
while also (potentially) matching against existing data. This operation can be used
for a number of different use cases.

### Bulk Update

The `lance.LanceDataset.update` method is useful for updating rows based on
a filter. However, if we want to replace existing rows with new rows then a `lance.LanceDataset.merge_insert`
operation would be more efficient:

```python
import lance

dataset = lance.dataset("./alice_and_bob.lance")
print(dataset.to_table().to_pandas())
#     name  age
# 0  Alice   20
# 1    Bob   30

# Change the ages of both Alice and Bob
new_table = pa.Table.from_pylist([{"name": "Alice", "age": 2},
                                  {"name": "Bob", "age": 3}])
# This will use `name` as the key for matching rows.  Merge insert
# uses a JOIN internally and so you typically want this column to
# be a unique key or id of some kind.
rst = dataset.merge_insert("name") \
       .when_matched_update_all() \
       .execute(new_table)
print(dataset.to_table().to_pandas())
#     name  age
# 0  Alice    2
# 1    Bob    3
```

Note that, similar to the update operation, rows that are modified will
be removed and inserted back into the table, changing their position to
the end. Also, the relative order of these rows could change because we
are using a hash-join operation internally.

### Insert if not Exists

Sometimes we only want to insert data if we haven't already inserted it
before. This can happen, for example, when we have a batch of data but
we don't know which rows we've added previously and we don't want to
create duplicate rows. We can use the merge insert operation to achieve
this:

```python
# Bob is already in the table, but Carla is new
new_table = pa.Table.from_pylist([{"name": "Bob", "age": 30},
                                  {"name": "Carla", "age": 37}])

dataset = lance.dataset("./alice_and_bob.lance")

# This will insert Carla but leave Bob unchanged
_ = dataset.merge_insert("name") \
       .when_not_matched_insert_all() \
       .execute(new_table)
# Verify that Carla was added but Bob remains unchanged
print(dataset.to_table().to_pandas())
#     name  age
# 0  Alice   20
# 1    Bob   30
# 2  Carla   37
```

### Update or Insert (Upsert)

Sometimes we want to combine both of the above behaviors. If a row
already exists we want to update it. If the row does not exist we want
to add it. This operation is sometimes called "upsert". We can use
the merge insert operation to do this as well:

```python
import lance
import pyarrow as pa

# Change Carla's age and insert David
new_table = pa.Table.from_pylist([{"name": "Carla", "age": 27},
                                  {"name": "David", "age": 42}])

dataset = lance.dataset("./alice_and_bob.lance")

# This will update Carla and insert David
_ = dataset.merge_insert("name") \
       .when_matched_update_all() \
       .when_not_matched_insert_all() \
       .execute(new_table)
# Verify the results
print(dataset.to_table().to_pandas())
#     name  age
# 0  Alice   20
# 1    Bob   30
# 2  Carla   27
# 3  David   42
```

### Replace a Portion of Data

A less common, but still useful, behavior can be to replace some region
of existing rows (defined by a filter) with new data. This is similar
to performing both a delete and an insert in a single transaction. For
example:

```python
import lance
import pyarrow as pa

new_table = pa.Table.from_pylist([{"name": "Edgar", "age": 46},
                                  {"name": "Francene", "age": 44}])

dataset = lance.dataset("./alice_and_bob.lance")
print(dataset.to_table().to_pandas())
#       name  age
# 0    Alice   20
# 1      Bob   30
# 2  Charlie   45
# 3    Donna   50

# This will remove anyone above 40 and insert our new data
_ = dataset.merge_insert("name") \
       .when_not_matched_insert_all() \
       .when_not_matched_by_source_delete("age >= 40") \
       .execute(new_table)
# Verify the results - people over 40 replaced with new data
print(dataset.to_table().to_pandas())
#        name  age
# 0     Alice   20
# 1       Bob   30
# 2     Edgar   46
# 3  Francene   44
```

## Reading Lance Dataset

To open a Lance dataset, use the `lance.dataset` function:

```python
import lance
ds = lance.dataset("s3://bucket/path/imagenet.lance")
# Or local path
ds = lance.dataset("./imagenet.lance")
```

!!! note

    Lance supports local file system, AWS `s3` and Google Cloud Storage(`gs`) as storage backends
    at the moment. Read more in [Object Store Configuration](object_store.md).

The most straightforward approach for reading a Lance dataset is to utilize the `lance.LanceDataset.to_table`
method in order to load the entire dataset into memory.

```python
table = ds.to_table()
```

Due to Lance being a high-performance columnar format, it enables efficient reading of subsets of the dataset by utilizing
**Column (projection)** push-down and **filter (predicates)** push-downs.

```python
table = ds.to_table(
    columns=["image", "label"],
    filter="label = 2 AND text IS NOT NULL",
    limit=1000,
    offset=3000)
```

Lance understands the cost of reading heavy columns such as `image`.
Consequently, it employs an optimized query plan to execute the operation efficiently.

### Iterative Read

If the dataset is too large to fit in memory, you can read it in batches
using the `lance.LanceDataset.to_batches` method:

```python
for batch in ds.to_batches(columns=["image"], filter="label = 10"):
    # do something with batch
    compute_on_batch(batch)
```

Unsurprisingly, `lance.LanceDataset.to_batches` takes the same parameters
as `lance.LanceDataset.to_table` function.

### Filter push-down

Lance embraces the utilization of standard SQL expressions as predicates for dataset filtering.
By pushing down the SQL predicates directly to the storage system,
the overall I/O load during a scan is significantly reduced.

Currently, Lance supports a growing list of expressions.

* `>`, `>=`, `<`, `<=`, `=`
* `AND`, `OR`, `NOT`
* `IS NULL`, `IS NOT NULL`
* `IS TRUE`, `IS NOT TRUE`, `IS FALSE`, `IS NOT FALSE`
* `IN`
* `LIKE`, `NOT LIKE`
* `regexp_match(column, pattern)`
* `CAST`

For example, the following filter string is acceptable:

```sql
((label IN [10, 20]) AND (note['email'] IS NOT NULL))
    OR NOT note['created']
```

Nested fields can be accessed using the subscripts. Struct fields can be
subscripted using field names, while list fields can be subscripted using
indices.

If your column name contains special characters or is a [SQL Keyword](https://docs.rs/sqlparser/latest/sqlparser/keywords/index.html),
you can use backtick (`` ` ``) to escape it. For nested fields, each segment of the
path must be wrapped in backticks.

```sql
`CUBE` = 10 AND `column name with space` IS NOT NULL
  AND `nested with space`.`inner with space` < 2
```

!!! warning

    Field names containing periods (`.`) are not supported.

Literals for dates, timestamps, and decimals can be written by writing the string
value after the type name. For example

```sql
date_col = date '2021-01-01'
and timestamp_col = timestamp '2021-01-01 00:00:00'
and decimal_col = decimal(8,3) '1.000'
```

For timestamp columns, the precision can be specified as a number in the type
parameter. Microsecond precision (6) is the default.

| SQL | Time unit |
|-----|-----------|
| `timestamp(0)` | Seconds |
| `timestamp(3)` | Milliseconds |
| `timestamp(6)` | Microseconds |
| `timestamp(9)` | Nanoseconds |

Lance internally stores data in Arrow format. The mapping from SQL types to Arrow is:

| SQL type | Arrow type |
|----------|------------|
| `boolean` | `Boolean` |
| `tinyint` / `tinyint unsigned` | `Int8` / `UInt8` |
| `smallint` / `smallint unsigned` | `Int16` / `UInt16` |
| `int` or `integer` / `int unsigned` or `integer unsigned` | `Int32` / `UInt32` |
| `bigint` / `bigint unsigned` | `Int64` / `UInt64` |
| `float` | `Float32` |
| `double` | `Float64` |
| `decimal(precision, scale)` | `Decimal128` |
| `date` | `Date32` |
| `timestamp` | `Timestamp` (1) |
| `string` | `Utf8` |
| `binary` | `Binary` |

(1) See precision mapping in previous table.

### Random read

One distinct feature of Lance, as columnar format, is that it allows you to read random samples quickly.

```python
# Access the 2nd, 101th and 501th rows
data = ds.take([1, 100, 500], columns=["image", "label"])
```

The ability to achieve fast random access to individual rows plays a crucial role in facilitating various workflows
such as random sampling and shuffling in ML training.
Additionally, it empowers users to construct secondary indices,
enabling swift execution of queries for enhanced performance.

## Table Maintenance

Some operations over time will cause a Lance dataset to have a poor layout. For
example, many small appends will lead to a large number of small fragments. Or
deleting many rows will lead to slower queries due to the need to filter out
deleted rows.

To address this, Lance provides methods for optimizing dataset layout.

### Compact data files

Data files can be rewritten so there are fewer files. When passing a
`target_rows_per_fragment` to `lance.dataset.DatasetOptimizer.compact_files`,
Lance will skip any fragments that are already above that row count, and rewrite
others. Fragments will be merged according to their fragment ids, so the inherent
ordering of the data will be preserved.

!!! note

    Compaction creates a new version of the table. It does not delete the old
    version of the table and the files referenced by it.

```python
import lance

dataset = lance.dataset("./alice_and_bob.lance")
dataset.optimize.compact_files(target_rows_per_fragment=1024 * 1024)
```

During compaction, Lance can also remove deleted rows. Rewritten fragments will
not have deletion files. This can improve scan performance since the soft deleted
rows don't have to be skipped during the scan.

When files are rewritten, the original row addresses are invalidated. This means the
affected files are no longer part of any ANN index if they were before. Because
of this, it's recommended to rewrite files before re-building indices.

<!-- TODO: remove this last comment once stable row ids are default. -->

### Cleanup old versions

Lance is an immutable format — every write creates a new version. The new version
only writes the data that changed, so an insert writes the new rows and an update
rewrites the affected columns for the affected rows. Even a delete creates a
small deletion file. However, old versions still reference the previous data
files, so those files are kept on disk until explicitly removed. Over time this
means storage grows with each operation — inserts, updates, and deletes alike.

Keeping old versions has important benefits: readers that opened an older version
can continue reading it without interference from concurrent writers, providing
snapshot isolation. Old versions also enable time travel queries, letting you
read the dataset as it existed at any prior point in time.

`cleanup_old_versions` deletes old version metadata and any data files that are
no longer referenced by any version, reclaiming the accumulated storage.

!!! warning

    Once old versions are cleaned up, time travel queries to those versions are
    no longer possible. Choose your retention window (`older_than`) accordingly —
    any version removed by cleanup cannot be recovered.

```python
import lance

dataset = lance.dataset("./my_dataset.lance")
dataset.cleanup_old_versions()
```

By default, versions older than 7 days are removed. You can override this with
the `older_than` parameter (a `timedelta`):

```python
from datetime import timedelta

dataset.cleanup_old_versions(older_than=timedelta(days=1))
```

!!! note

    Tagged versions are exempt from cleanup. See [Tags and Branches](tags_and_branches.md)
    for details.

By default, Lance only removes files that it can **verify** are no longer needed.
A file is verified when Lance can see that it was referenced by an older version
and is no longer referenced by any newer version. However, some orphaned files
cannot be verified this way — for example, files left behind by aborted or failed
commits that were never recorded in any version. These files are
indistinguishable from files being written by an in-progress operation.

Cleanup will never delete the current (active) version. This means passing
`older_than=timedelta(0)` is safe and will delete all versions except the current
one.

The `delete_unverified` flag enables a more aggressive strategy that will also
delete these unverified files:

```python
dataset.cleanup_old_versions(
    older_than=timedelta(hours=2),
    delete_unverified=True,
)
```

!!! danger

    Only use `delete_unverified=True` when you are confident that no other
    concurrent operation has been in-progress for longer than the `older_than`
    duration. Lance uses the file's age to decide whether an unverified file is
    safe to remove, so any operation that is still running past the `older_than`
    window risks having its files deleted out from under it.

    In particular, combining `delete_unverified=True` with `older_than=timedelta(0)`
    is **extremely dangerous** — if any other operation is in-progress at all,
    its data files may be deleted, leading to dataset corruption.

### Automatic cleanup

Instead of calling `cleanup_old_versions` manually, you can configure Lance to
clean up old versions automatically during writes. When auto cleanup is enabled,
Lance will run cleanup every *N* commits (the **interval**), removing versions
older than a specified duration.

Auto cleanup can be enabled when creating a new dataset:

```python
import lance
import pyarrow as pa
from lance.dataset import AutoCleanupConfig

table = pa.table({"id": range(100)})
ds = lance.write_dataset(
    table,
    "./my_dataset.lance",
    auto_cleanup_options=AutoCleanupConfig(
        interval=20,             # run cleanup every 20 commits
        older_than_seconds=3600, # remove versions older than 1 hour
    ),
)
```

Or enabled on an existing dataset:

```python
ds = lance.dataset("./my_dataset.lance")
ds.optimize.enable_auto_cleanup(
    AutoCleanupConfig(
        interval=20,
        older_than_seconds=3600,
    )
)
```

And disabled again:

```python
ds.optimize.disable_auto_cleanup()
```

Auto cleanup parameters can also be set directly via dataset config keys:

```python
ds.update_config({
    "lance.auto_cleanup.interval": "20",
    "lance.auto_cleanup.older_than": "3600s",
})
```

!!! warning

    Auto cleanup runs as part of the commit path. If your writer does not have
    delete permissions, or you are doing high-frequency writes where the extra
    latency matters, pass `skip_auto_cleanup=True` to `write_dataset` to skip it
    on a per-write basis.

### Other cleanup strategies

It is common to run cleanup as a periodic background task on a dedicated server
(for example, via a cron job or scheduled workflow). This keeps cleanup off the
write path entirely, avoiding any impact to write latency, but requires setting
up and maintaining additional infrastructure.


================================================
FILE: docs/src/guide/tags_and_branches.md
================================================
# Manage Tags and Branches

Lance provides Git-like tag and branch capabilities through the `LanceDataset.tags` and `LanceDataset.branches` properties.

## Tags
Tags label specific versions within a branch's history.

`Tags` are particularly useful for tracking the evolution of datasets,
especially in machine learning workflows where datasets are frequently updated.
For example, you can `create`, `update`,
and `delete` or `list` tags.

The `reference` parameter (used in `create`, `update`, and `checkout_version`) accepts:

- An **integer**: version number in the **current branch** (e.g., `1`)
- A **string**: tag name (e.g., `"stable"`)
- A **tuple** `(branch_name, version)`: a specific version in a named branch
  - `(None, 2)` means version 2 on the default branch
  - `("main", 2)` means version 2 on the default branch (explicit)
  - `("experiment", 3)` means version 3 on the experiment branch
  - `("branch-name", None)` means the latest version on that branch

In reference contexts, `"main"` is an alias for the default branch and is equivalent to `None`.

!!! note

    Creating or deleting tags does not generate new dataset versions.
    Tags exist as auxiliary metadata stored in a separate directory.

```python
import lance
import pyarrow as pa

ds = lance.dataset("./tags.lance")
print(len(ds.versions()))
# 2
print(ds.tags.list())
# {}
ds.tags.create("v1-prod", (None, 1))
print(ds.tags.list())
# {'v1-prod': {'version': 1, 'created_at': ..., 'updated_at': ..., 'manifest_size': ...}}
ds.tags.update("v1-prod", (None, 2))
print(ds.tags.list())
# {'v1-prod': {'version': 2, 'created_at': ..., 'updated_at': ..., 'manifest_size': ...}}
ds.tags.delete("v1-prod")
print(ds.tags.list())
# {}
print(ds.tags.list_ordered())
# []
ds.tags.create("v1-prod", (None, 1))
print(ds.tags.list_ordered())
# [('v1-prod', {'version': 1, 'created_at': ..., 'updated_at': ..., 'manifest_size': ...})]
ds.tags.update("v1-prod", (None, 2))
print(ds.tags.list_ordered())
# [('v1-prod', {'version': 2, 'created_at': ..., 'updated_at': ..., 'manifest_size': ...})]
ds.tags.delete("v1-prod")
print(ds.tags.list_ordered())
# []
```

!!! note

    Tagged versions are exempted from the `LanceDataset.cleanup_old_versions()`
    process.

    To remove a version that has been tagged, you must first `LanceDataset.tags.delete()`
    the associated tag.

## Branches

Branches manage parallel lines of dataset evolution. You can create a branch from an existing version or tag, read and write to it independently, and checkout different branches. You can `create`, `delete`, `list`, and `checkout` branches.

The `reference` parameter works the same as for Tags (see above).

!!! note

    Creating or deleting branches does not generate new dataset versions.
    New versions are created by writes (append/overwrite/index operations).

    Each branch maintains its own linear version history, so version numbers may overlap across branches. Use `(branch_name, version_number)` tuples as global identifiers for operations like `checkout_version` and `tags.create`.

    `"main"` is reserved for the default branch. Use `"main"` or `None` when referring to the default branch in reference tuples or checkout APIs, but choose a different name when creating, deleting, or updating branches.

### Create and checkout branches
```python
import lance
import pyarrow as pa

# Open dataset
ds = lance.dataset("/tmp/test.lance")

# Create branch from the currently checked-out version
experiment_branch = ds.create_branch("experiment")
experimental_data = pa.Table.from_pydict({"a": [11], "b": [12]})
lance.write_dataset(experimental_data, experiment_branch, mode="append")

# Create tag on the latest version of the experimental branch
ds.tags.create("experiment-rc", ("experiment", None))

# Checkout by tag name
experiment_rc = ds.checkout_version("experiment-rc")
# Checkout the latest version of the experimental branch by tuple
experiment_latest = ds.checkout_version(("experiment", None))
# Checkout the latest version of the default branch explicitly
main_latest = ds.checkout_version(("main", None))

# Create a new branch from a tag
new_experiment = ds.create_branch("new-experiment", "experiment-rc")
```

### List branches
```python
print(ds.branches.list())
# {'experiment': {...}, 'new-experiment': {...}}
```

### Delete a branch
```python
# Ensure the branch is no longer needed before deletion
ds.branches.delete("experiment")
print(ds.branches.list_ordered(order="desc"))
# {'new-experiment': {'parent_branch': 'experiment', 'parent_version': 2, 'create_at': ..., 'manifest_size': ...}, ...}
```

!!! note

    Branches hold references to data files. Lance ensures that cleanup does not delete files still referenced by any branch.

    Delete unused branches to allow their referenced files to be cleaned up by `cleanup_old_versions()`.


================================================
FILE: docs/src/guide/tokenizer.md
================================================
# Tokenizers

Currently, Lance has built-in support for ICU, Jieba, and Lindera. ICU uses built-in segmenter data. Jieba and Lindera require external language models.
If tokenization is needed, you can download language models by yourself.
You can specify the location where the language models are stored by setting the environment variable LANCE_LANGUAGE_MODEL_HOME.
If it's not set, the default value is

```bash
${system data directory}/lance/language_models
```

It also supports configuring user dictionaries,
which makes it convenient for users to expand their own dictionaries without retraining the language models.

## Inspect Query Tokenization

Use `lance.tokenize` to inspect the tokens that a full-text query will produce
without creating a dataset or index:

```python
import lance

tokens = lance.tokenize("the Cats and Dogs")
[(token.text, token.position) for token in tokens]
# [("cat", 0), ("dog", 2)]
```

Positions start at the first retained query token and preserve gaps left by stop
word removal and other filters. This is the same representation used for phrase
matching. The function accepts the tokenizer-related options supported by
`LanceDataset.create_scalar_index`, including custom stop words, n-grams, and the
code analyzer:

```python
tokens = lance.tokenize(
    "getUserName::value42",
    analyzer="code",
    split_identifiers=True,
    index_operators=True,
)
```

Options set to `None` use the selected analyzer profile's default. For example,
the code analyzer disables stemming and stop-word removal unless explicitly
overridden. The exception is `max_token_length`: omitting it keeps the default
length limit of 40, while `max_token_length=None` disables the limit.

## ICU Tokenizer

ICU uses Unicode word boundary rules and bundled dictionary data for complex scripts. It is useful for mixed-language text and does not require downloading a language model.

```python
ds.create_scalar_index("text", "INVERTED", base_tokenizer="icu")
```

Use `icu/split` when mixed-language text also contains punctuation-delimited identifiers that should be searchable by part.

```python
ds.create_scalar_index("text", "INVERTED", base_tokenizer="icu/split")
```

## Language Models of Jieba

### Downloading the Model

```bash
python -m lance.download jieba
```

The language model is stored by default in `${LANCE_LANGUAGE_MODEL_HOME}/jieba/default`.

### Using the Model

```python
ds.create_scalar_index("text", "INVERTED", base_tokenizer="jieba/default")
```

### User Dictionaries

Create a file named config.json in the root directory of the current model.

```json
{
    "main": "dict.txt",
    "users": ["path/to/user/dict.txt"]
}
```

- The "main" field is optional. If not filled, the default is "dict.txt".
- "users" is the path of the user dictionary. For the format of the user dictionary, please refer to https://github.com/messense/jieba-rs/blob/main/jieba/src/data/dict.txt.

## Language Models of Lindera

### Downloading the Model

```bash
python -m lance.download lindera -l [ipadic|ko-dic|unidic]
```

Note that the language models of Lindera need to be compiled. Please install lindera-cli first. For detailed steps, please refer to https://github.com/lindera/lindera/tree/main/lindera-cli.

The language model is stored by default in ${LANCE_LANGUAGE_MODEL_HOME}/lindera/[ipadic|ko-dic|unidic]

### Using the Model

```python
ds.create_scalar_index("text", "INVERTED", base_tokenizer="lindera/ipadic")
```

### User Dictionaries

Create a file named config.yml in the root directory of your model, or specify a custom YAML file using the `LINDERA_CONFIG_PATH` environment variable.
If both are provided, the config.yml in the root directory will be used.
For more detailed configuration methods, see the lindera documentation at https://github.com/lindera/lindera/.

```yaml
segmenter:
    mode: "normal"
    dictionary: /path/to/lindera/ipadic/main
```

## Create your own language model

Put your language model into `LANCE_LANGUAGE_MODEL_HOME`.


================================================
FILE: docs/src/guide/.pages
================================================
nav:
  - Read and Write: read_and_write.md
  - Data Types: data_types.md
  - Data Evolution: data_evolution.md
  - Blob API: blob.md
  - JSON Support: json.md
  - Tags and Branches: tags_and_branches.md
  - Object Store Configuration: object_store.md
  - Observability: observability.md
  - Distributed Write: distributed_write.md
  - Distributed Indexing: distributed_indexing.md
  - Migration Guide: migration.md
  - Performance Guide: performance.md
  - Tokenizer: tokenizer.md
  - Extension Arrays: arrays.md
