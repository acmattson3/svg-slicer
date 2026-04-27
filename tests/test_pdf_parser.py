from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import LineString, Polygon

from svg_slicer.artwork_parser import parse_artwork


def test_parse_pdf_hybrid_imports_vectors_and_text(tmp_path: Path, slicer_config) -> None:
    fitz = pytest.importorskip("fitz")

    path = tmp_path / "hybrid.pdf"
    doc = fitz.open()
    page = doc.new_page(width=120, height=90)
    page.draw_rect(fitz.Rect(10, 10, 50, 40), color=(1, 0, 0), fill=(0, 0, 0), width=1)
    page.insert_text((10, 70), "Hi", fontsize=12, color=(0, 0, 1))
    doc.save(str(path))
    doc.close()

    shapes = parse_artwork(path, slicer_config.sampling, pdf_page=1)

    assert any(isinstance(shape.geometry, Polygon) for shape in shapes)
    assert any(isinstance(shape.geometry, LineString) and shape.stroke_width is not None for shape in shapes)


def test_parse_pdf_uses_selected_page(tmp_path: Path, slicer_config) -> None:
    fitz = pytest.importorskip("fitz")

    path = tmp_path / "pages.pdf"
    doc = fitz.open()
    first = doc.new_page(width=100, height=100)
    first.draw_rect(fitz.Rect(5, 5, 20, 20), fill=(0, 0, 0))
    second = doc.new_page(width=100, height=100)
    second.draw_rect(fitz.Rect(40, 40, 80, 80), fill=(0, 0, 0))
    doc.save(str(path))
    doc.close()

    shapes = parse_artwork(path, slicer_config.sampling, pdf_page=2)

    minx, miny, maxx, maxy = shapes[0].geometry.bounds
    assert minx == pytest.approx(40.0)
    assert miny == pytest.approx(40.0)
    assert maxx == pytest.approx(80.0)
    assert maxy == pytest.approx(80.0)
