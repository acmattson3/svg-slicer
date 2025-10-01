# SVG Slicer for Pen Plotter 3D Printers

This project provides a command-line slicer that converts filled shapes from an SVG file into G-code suitable for a pen attachment mounted on a modified 3D printer. Instead of extruding filament, the slicer treats the printer as a 2D plotter, automatically scaling the artwork to fit your printable area, converting the design to grayscale, and producing rectilinear infill that varies with shape brightness.

## Features

- Loads configuration from YAML with typical printer settings (bed size, safe offsets, feedrates, lift heights, start/end sequences).
- Parses SVG documents (via `svgelements`) and converts fills to grayscale so the line density reflects the shape brightness.
- Uniformly scales and flips the design into printer coordinates while keeping within the configured X/Y borders.
- Traces perimeter bands with configurable thickness, duplicating outline passes for stroke-width coverage and hatching stroke/fill regions (respecting SVG stroke widths when present) for solid coverage.
- Generates rectilinear cross-hatch infill (configurable angles) for each filled shape—darker shapes receive denser line spacing, and the slicer greedily chains line segments across both orientations to minimise pen lifts.
- Emits travel/drawing G-code with configurable speeds and Z-lift for pen-up travel moves.
- Optional matplotlib preview plotting only the drawing moves on a white canvas.
- Can save the preview to an image file for headless environments.
- Automatically converts thick SVG strokes into filled regions and skips time-consuming hatching for ultra-thin details.
- Roadmap item: add glyph conversion so raw `<text>` elements can be plotted without manual outline conversion.

## Installation

System packages are used for dependencies. On Ubuntu/WSL you can install them with:

```bash
sudo apt-get install python3-svgelements python3-shapely python3-yaml python3-matplotlib
```

No additional virtual environment is required; the CLI runs with the system Python (`/usr/bin/python3`).

## Configuration

Edit `config.yaml` to match your machine. Key sections include:

- `printer.bed_size_mm`: overall bed dimensions (default 220 mm square).
- `printer.origin_offsets_mm`: usable area (defaults to a 14 mm border on all sides).
- `printer.z_heights_mm` and `printer.z_lift_height_mm`: drawing and travel heights (default travel/lift 4 mm).
- `printer.feedrates_mm_s`: drawing, travel, and Z feedrates expressed in mm/s (defaults to 175 mm/s for XY moves).
- `perimeter`: thickness (defaults to 0.45 mm), density (defaults to fully solid), and the minimum width at which hatch infill is attempted (default 0.8 mm).
- `infill`: base line spacing and min/max density along with the infill angles (1 mm spacing at 100% density by default).
- `sampling.segment_length_tolerance_mm`: detail level when converting curves to polygons.
- `sampling.outline_simplify_tolerance_mm`: optional smoothing for outlines to reduce extremely small linear segments.

## Usage

```bash
/usr/bin/python3 -m svg_slicer path/to/art.svg \
  --config config.yaml \
  --output out.gcode \
  --preview-file preview.png
```

Flags:

- `--preview` opens an interactive matplotlib plot of the drawing moves.
- `--preview-file` saves the preview image instead (helpful on headless systems).
- `--log-level` adjusts verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

Generated G-code starts with the configured start-up sequence (home → lift → move to front-left origin), traces perimeter bands, then applies cross-hatch infill with spacing tied to grayscale brightness while respecting SVG stacking order so upper shapes mask lower ones.

## Notes

- Filled regions and SVG strokes are both converted into hatchable toolpaths.
- The slicer assumes the printer uses absolute coordinates and millimeters.
- Preview rendering ignores travel moves so you only see actual drawing strokes on a white background.
- For very light fills the density is clamped to `infill.min_density`, ensuring at least a faint hatch for the pen plotter.
