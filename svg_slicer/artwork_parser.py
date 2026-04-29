from __future__ import annotations

from pathlib import Path
from typing import List

from .config import SamplingConfig
from .svg_parser import (
    ShapeGeometry,
    _raster_pil_image_to_shape_geometries,
    _vectorize_pil_image_to_shape_geometries,
    parse_svg,
)


_BITMAP_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}


def _parse_bitmap(path: Path, sampling: SamplingConfig) -> List[ShapeGeometry]:
    try:
        from PIL import Image as PILImage
    except Exception as exc:
        raise RuntimeError("Bitmap import requires Pillow. Install it with `pip install Pillow`.") from exc

    with PILImage.open(path) as pil_image:
        pil_image.load()
        bbox = (0.0, 0.0, float(pil_image.width), float(pil_image.height))
        helper = (
            _vectorize_pil_image_to_shape_geometries
            if getattr(sampling, "image_mode", "raster") == "vectorize"
            else _raster_pil_image_to_shape_geometries
        )
        return helper(
            pil_image,
            bbox,
            sampling,
            clip_geom=None,
        )


def parse_artwork(
    path: str | Path,
    sampling: SamplingConfig,
    *,
    pdf_page: int = 1,
    force_hershey_text: bool = False,
) -> List[ShapeGeometry]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".svg":
        return parse_svg(str(source), sampling, force_hershey_text=force_hershey_text)
    if suffix == ".pdf":
        from .pdf_parser import parse_pdf

        return parse_pdf(
            str(source),
            sampling,
            page_number=pdf_page,
            force_hershey_text=force_hershey_text,
        )
    if suffix in _BITMAP_SUFFIXES:
        return _parse_bitmap(source, sampling)
    raise ValueError(
        f"Unsupported artwork file type '{source.suffix}'. Expected .svg, .pdf, or a bitmap image."
    )
