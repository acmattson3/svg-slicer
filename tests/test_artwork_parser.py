from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PILImage
from shapely.geometry import Polygon

from svg_slicer.artwork_parser import parse_artwork


def test_parse_artwork_accepts_bitmap_image(tmp_path: Path, slicer_config) -> None:
    pytest.importorskip("cv2")
    slicer_config.sampling.image_mode = "vectorize"

    image_path = tmp_path / "photo.png"
    image = PILImage.new("RGBA", (48, 32), (255, 255, 255, 255))
    for x in range(8, 40):
        for y in range(6, 26):
            image.putpixel((x, y), (0, 0, 0, 255))
    image.save(image_path)

    shapes = parse_artwork(image_path, slicer_config.sampling)

    assert shapes
    assert all(isinstance(shape.geometry, Polygon) for shape in shapes)
    assert all(shape.toolpath_tag == "image-vector" for shape in shapes)
