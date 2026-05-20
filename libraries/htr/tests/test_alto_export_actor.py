import io

import numpy as np
from PIL import Image


def test_alto_export_actor_emits_xml():
    from htr._columns import pack
    from htr.actors.alto_export import AltoExportActor

    img = Image.new("RGB", (200, 100), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    actor = AltoExportActor()
    batch = {
        "key": np.array(["A0060198/00012.jpg"], dtype=object),
        "image_bytes": np.array([buf.getvalue()], dtype=object),
        "regions": np.array([pack([])], dtype=object),
        "transcribed": np.array([pack([])], dtype=object),
    }
    out = actor(batch)
    assert "alto_xml" in out
    assert b"<alto" in out["alto_xml"][0]
    assert out["output_key"][0] == "A0060198/00012.xml"


def test_fake_alto_actor():
    from htr.actors.fake import FakeAltoActor

    img = Image.new("RGB", (200, 100), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    actor = FakeAltoActor()
    batch = {
        "key": np.array(["A0060198/00012.jpg"], dtype=object),
        "image_bytes": np.array([buf.getvalue()], dtype=object),
    }
    out = actor(batch)
    assert "alto_xml" in out
    assert out["output_key"][0] == "A0060198/00012.xml"
    assert b"FAKE" in out["alto_xml"][0]
