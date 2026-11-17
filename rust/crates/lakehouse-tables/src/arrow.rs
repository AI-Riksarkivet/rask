//! Rows as Arrow: the IPC stream a write door takes, and the IPC answer a `/query` door gives, validated in full.
//!
//! A write body is a **stream** (`spec.yaml`'s `application/vnd.apache.arrow.stream` bodies). A query answer is an IPC
//! **file** by the spec, and a stream is read too, as the Python reader reads both. Decoding validates every array (its
//! offsets, its UTF-8), because framing that parses says nothing about the buffers it frames.

use arrow_array::{Array, RecordBatch};
use arrow_ipc::reader::{FileReader, StreamReader};
use arrow_ipc::writer::StreamWriter;
use arrow_schema::{ArrowError, SchemaRef};
use std::io::Cursor;

/// The IPC file format's leading magic; a stream opens with a message length instead.
const FILE_MAGIC: &[u8] = b"ARROW1";

/// Rows of one schema, in batches.
#[derive(Debug, Clone, PartialEq)]
pub struct Rows {
    pub schema: SchemaRef,
    pub batches: Vec<RecordBatch>,
}

impl Rows {
    /// No rows, of `schema`: the body a create sends.
    pub fn empty(schema: SchemaRef) -> Self {
        Rows { schema, batches: Vec::new() }
    }

    pub fn num_rows(&self) -> usize {
        self.batches.iter().map(RecordBatch::num_rows).sum()
    }

    /// The first `limit` rows.
    pub fn head(self, limit: usize) -> Self {
        let mut left = limit;
        let mut batches = Vec::new();
        for batch in self.batches {
            if left == 0 {
                break;
            }
            let take = left.min(batch.num_rows());
            batches.push(batch.slice(0, take));
            left -= take;
        }
        Rows { schema: self.schema, batches }
    }

    /// These rows followed by `more`, or `None` when `more` holds rows of another schema.
    pub fn joined(mut self, more: Rows) -> Option<Self> {
        if more.num_rows() > 0 {
            if more.schema != self.schema {
                return None;
            }
            self.batches.extend(more.batches);
        }
        Some(self)
    }

    /// The bytes the rows' buffers hold, as their arrays measure them: what a page's row width is taken from.
    pub fn nbytes(&self) -> usize {
        self.batches
            .iter()
            .flat_map(RecordBatch::columns)
            .map(|column| column.to_data().get_slice_memory_size().unwrap_or_else(|_| column.get_array_memory_size()))
            .sum()
    }
}

impl From<RecordBatch> for Rows {
    fn from(batch: RecordBatch) -> Self {
        Rows {
            schema: batch.schema(),
            batches: vec![batch],
        }
    }
}

/// `rows` as an Arrow IPC stream, uncompressed.
pub fn encode_stream(rows: &Rows) -> Result<Vec<u8>, ArrowError> {
    let mut writer = StreamWriter::try_new(Vec::new(), &rows.schema)?;
    for batch in &rows.batches {
        writer.write(batch)?;
    }
    writer.into_inner()
}

/// A catalog's Arrow IPC answer, file or stream, with every array validated.
///
/// A stream must be the whole answer: a reader stops at the first end-of-stream marker, so bytes after it would be
/// dropped unread.
pub fn decode_answer(bytes: &[u8]) -> Result<Rows, ArrowError> {
    if bytes.starts_with(FILE_MAGIC) {
        let reader = FileReader::try_new(Cursor::new(bytes), None)?;
        let schema = reader.schema();
        let batches = reader.collect::<Result<Vec<_>, _>>()?;
        return Ok(Rows { schema, batches });
    }
    let mut cursor = Cursor::new(bytes);
    let (schema, batches) = {
        let reader = StreamReader::try_new(&mut cursor, None)?;
        let schema = reader.schema();
        (schema, reader.collect::<Result<Vec<_>, _>>()?)
    };
    let unread = bytes.len().saturating_sub(usize::try_from(cursor.position()).unwrap_or(usize::MAX));
    if unread > 0 {
        return Err(ArrowError::IpcError(format!("{unread} bytes follow the end of the stream")));
    }
    Ok(Rows { schema, batches })
}
