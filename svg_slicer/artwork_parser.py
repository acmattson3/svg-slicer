from __future__ import annotations

from pathlib import Path
from typing import List

from .config import SamplingConfig
from .svg_parser import ShapeGeometry, parse_svg


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
    raise ValueError(f"Unsupported artwork file type '{source.suffix}'. Expected .svg or .pdf.")
