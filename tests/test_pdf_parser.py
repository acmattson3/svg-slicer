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


def test_parse_pdf_ignores_full_page_border_rectangle(tmp_path: Path, slicer_config) -> None:
    fitz = pytest.importorskip("fitz")

    path = tmp_path / "page_border.pdf"
    doc = fitz.open()
    page = doc.new_page(width=100, height=100)
    page.draw_rect(page.rect, color=(0, 0, 0), width=1)
    page.draw_rect(fitz.Rect(10, 10, 20, 20), fill=(0, 0, 0))
    doc.save(str(path))
    doc.close()

    shapes = parse_artwork(path, slicer_config.sampling, pdf_page=1)

    assert shapes
    bounds = [shape.geometry.bounds for shape in shapes]
    minx = min(bound[0] for bound in bounds)
    miny = min(bound[1] for bound in bounds)
    maxx = max(bound[2] for bound in bounds)
    maxy = max(bound[3] for bound in bounds)
    assert minx >= 10.0 - 1e-6
    assert miny >= 10.0 - 1e-6
    assert maxx <= 20.0 + 1e-6
    assert maxy <= 20.0 + 1e-6


def test_parse_pdf_merges_adjacent_stroke_segments(tmp_path: Path, slicer_config) -> None:
    fitz = pytest.importorskip("fitz")

    path = tmp_path / "segmented_line.pdf"
    doc = fitz.open()
    page = doc.new_page(width=160, height=60)
    for start_x in range(10, 110, 10):
        page.draw_line((start_x, 20), (start_x + 10, 20), color=(0, 0, 0), width=1)
    doc.save(str(path))
    doc.close()

    shapes = parse_artwork(path, slicer_config.sampling, pdf_page=1)
    line_shapes = [shape for shape in shapes if isinstance(shape.geometry, LineString)]

    assert len(line_shapes) == 1
    minx, miny, maxx, maxy = line_shapes[0].geometry.bounds
    assert minx == pytest.approx(10.0)
    assert miny == pytest.approx(20.0)
    assert maxx == pytest.approx(110.0)
    assert maxy == pytest.approx(20.0)


def test_parse_pdf_hershey_text_fits_span_bounds(tmp_path: Path, slicer_config) -> None:
    fitz = pytest.importorskip("fitz")

    path = tmp_path / "text_fit.pdf"
    doc = fitz.open()
    page = doc.new_page(width=180, height=90)
    page.insert_text((20, 50), "Wide Text", fontsize=24, color=(0, 0, 0))
    doc.save(str(path))
    doc.close()

    doc = fitz.open(str(path))
    span_bbox = doc[0].get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]["bbox"]
    doc.close()

    shapes = parse_artwork(path, slicer_config.sampling, pdf_page=1)
    line_shapes = [shape for shape in shapes if isinstance(shape.geometry, LineString)]
    assert line_shapes

    bounds = [shape.geometry.bounds for shape in line_shapes]
    minx = min(bound[0] for bound in bounds)
    miny = min(bound[1] for bound in bounds)
    maxx = max(bound[2] for bound in bounds)
    maxy = max(bound[3] for bound in bounds)

    assert minx >= span_bbox[0] - 1e-6
    assert miny >= span_bbox[1] - 1e-6
    assert maxx <= span_bbox[2] + 1e-6
    assert maxy <= span_bbox[3] + 1e-6


def test_parse_pdf_hershey_text_normalizes_tab_split_spans(tmp_path: Path, slicer_config) -> None:
    fitz = pytest.importorskip("fitz")

    normal_path = tmp_path / "normal_sentence.pdf"
    spaced_path = tmp_path / "spaced_sentence.pdf"
    for path, text in [
        (normal_path, "The dog ran fast"),
        (spaced_path, "  The\tdog ran   fast  "),
    ]:
        doc = fitz.open()
        page = doc.new_page(width=260, height=90)
        page.insert_text((20, 50), text, fontsize=18, color=(0, 0, 0))
        doc.save(str(path))
        doc.close()

    normal_shapes = [
        shape for shape in parse_artwork(normal_path, slicer_config.sampling, pdf_page=1)
        if isinstance(shape.geometry, LineString)
    ]
    spaced_shapes = [
        shape for shape in parse_artwork(spaced_path, slicer_config.sampling, pdf_page=1)
        if isinstance(shape.geometry, LineString)
    ]

    normal_width = max(shape.geometry.bounds[2] for shape in normal_shapes) - min(
        shape.geometry.bounds[0] for shape in normal_shapes
    )
    spaced_width = max(shape.geometry.bounds[2] for shape in spaced_shapes) - min(
        shape.geometry.bounds[0] for shape in spaced_shapes
    )

    assert spaced_width == pytest.approx(normal_width, rel=0.05)
