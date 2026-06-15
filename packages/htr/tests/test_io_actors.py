from pathlib import Path

import numpy as np


def test_page_loader_actor_reads_bytes(tmp_path: Path):
    import io

    from PIL import Image

    from htr.actors.io import PageLoaderActor
    from storage import FSSource

    # PageLoaderActor validates each page with Image.verify(), so the fixture
    # must be a real image rather than arbitrary bytes. Use a tiny valid JPEG and
    # assert the actor returns the exact bytes it read (it does not re-encode).
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(buf, format="JPEG")
    image_bytes = buf.getvalue()

    (tmp_path / "A0060198").mkdir()
    (tmp_path / "A0060198" / "00001.jpg").write_bytes(image_bytes)

    actor = PageLoaderActor(source=FSSource(root=tmp_path))
    batch = {"key": np.array(["A0060198/00001.jpg"], dtype=object)}
    out = actor(batch)
    assert "image_bytes" in out
    assert out["image_bytes"][0] == image_bytes


def test_alto_writer_actor_writes(tmp_path: Path):
    from htr.actors.io import AltoWriterActor
    from storage import FSSink

    actor = AltoWriterActor(sink=FSSink(root=tmp_path))
    batch = {
        "output_key": np.array(["foo/bar.xml"], dtype=object),
        "alto_xml": np.array([b"<alto/>"], dtype=object),
    }
    out = actor(batch)
    assert (tmp_path / "foo" / "bar.xml").read_bytes() == b"<alto/>"
    assert "output_key" in out
