"""FakeAltoActor — emits a stub ALTO without running any models. Smoke-test pipeline."""

from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image


_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="http://www.loc.gov/standards/alto/ns-v4# http://www.loc.gov/standards/alto/v4/alto-4-4.xsd">
  <Description><MeasurementUnit>pixel</MeasurementUnit>
    <sourceImageInformation><fileName>{name}</fileName></sourceImageInformation>
    <Processing ID="P1"><processingSoftware>
      <softwareName>ra-htr-fake</softwareName><softwareVersion>0.1.0</softwareVersion>
    </processingSoftware></Processing>
  </Description>
  <Layout>
    <Page ID="page_1" PHYSICAL_IMG_NR="1" WIDTH="{w}" HEIGHT="{h}">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="{w}" HEIGHT="{h}">
        <TextBlock ID="block_1" HPOS="0" VPOS="0" WIDTH="{w}" HEIGHT="{h}">
          <TextLine ID="line_1" HPOS="0" VPOS="0" WIDTH="{w}" HEIGHT="{h}">
            <String CONTENT="FAKE" HPOS="0" VPOS="0" WIDTH="{w}" HEIGHT="{h}" WC="1.0"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
"""


class FakeAltoActor:
    def __init__(self, **_unused: Any) -> None:  # noqa: ANN401
        pass

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        out_keys: list[str] = []
        out_xml: list[bytes] = []
        for key, img_bytes in zip(batch["key"], batch["image_bytes"], strict=True):
            with Image.open(BytesIO(img_bytes)) as img:
                w, h = img.size
            name = key.rsplit("/", 1)[-1]
            xml = _TEMPLATE.format(name=name, w=w, h=h)
            out_keys.append(key.rsplit(".", 1)[0] + ".xml")
            out_xml.append(xml.encode("utf-8"))
        batch["output_key"] = np.array(out_keys, dtype=object)
        batch["alto_xml"] = np.array(out_xml, dtype=object)
        return batch
