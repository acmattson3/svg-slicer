# SVG Slicer for Pen Plotter 3D Printers

This project provides a command-line slicer that converts filled shapes from an SVG file into G-code suitable for a pen attachment mounted on a modified 3D printer. Instead of extruding filament, the slicer treats the printer as a 2D plotter, automatically scaling the artwork to fit your printable area, converting the design to grayscale, and producing rectilinear infill that varies with shape brightness.

## Features

- PySide6-powered GUI lets you queue multiple SVGs, scale and rotate each model interactively on a virtual build plate, preview toolpaths, and export G-code in one flow.
- Loads configuration from YAML with typical printer settings (bed size, safe offsets, feedrates, lift heights, start/end sequences).
- Parses SVG documents (via `svgelements`) and converts fills to grayscale so the line density reflects the shape brightness when running in black-and-white mode.
- Optional colour-mode maps each SVG fill/stroke to the nearest entry in your configured pen palette, batches every assigned colour, and inserts your custom pause script between colours (defaults to `M600`) in least-used-first order.
- Uniformly scales and flips the design into printer coordinates while keeping within the configured X/Y borders.
- Traces perimeter bands with configurable thickness, duplicating outline passes for stroke-width coverage and hatching stroke/fill regions (respecting SVG stroke widths when present) for solid coverage.
- Generates rectilinear cross-hatch infill (configurable angles) for each filled shape—darker shapes receive denser line spacing, and the slicer greedily chains line segments across both orientations to minimise pen lifts.
- Emits travel/drawing G-code with configurable speeds and Z-lift for pen-up travel moves.
- Optional matplotlib preview plotting only the drawing moves on a white canvas.
- Can save the preview to an image file for headless environments.
- Automatically converts thick SVG strokes into filled regions and skips time-consuming hatching for ultra-thin details.
- Converts raw `<text>` elements into outline paths (via Matplotlib's font tooling) so you can plot native SVG text without manually outlining first.

## Installation

System packages are used for dependencies. On Ubuntu/WSL you can install them with:

```bash
sudo apt-get install python3-svgelements python3-shapely python3-yaml python3-matplotlib
```

No additional virtual environment is required; the CLI runs with the system Python (`/usr/bin/python3`).

## Configuration

Edit `config.yaml` to match your machine. Printer settings are organised into named profiles so you can switch between different hardware or toolheads with a flag:

- `default_printer`: optional name of the profile selected when `--printer-profile` is omitted (falls back to the first profile otherwise).
- `printers.<profile>.bed_size_mm`: overall bed dimensions for each profile.
- `printers.<profile>.origin_offsets_mm`: usable XY area relative to the machine origin.
- `printers.<profile>.z_heights_mm` and `printers.<profile>.z_lift_height_mm`: commanded pen-down, travel, and lift heights.
- `printers.<profile>.feedrates_mm_s`: drawing, travel, and Z feedrates expressed in mm/s.
- `printers.<profile>.color_mode`: enable (`true`) or disable (`false`) the colour workflow for that profile.
- `printers.<profile>.available_colors`: ordered list of hex colours (e.g. `["#000000", "#FF0000"]`) representing the pens you have on hand; the slicer snaps SVG colours to the closest entry.
- `printers.<profile>.pause_gcode`: commands executed between colour batches (e.g. manual pause macro with safe Z lift and parking move); defaults to `M600` when omitted.
- `perimeter`: thickness, density, and the minimum width at which hatch infill is attempted.
- `infill`: base line spacing and min/max density along with the infill angles.
- `sampling.segment_length_tolerance_mm`: detail level when converting curves to polygons.
- `sampling.outline_simplify_tolerance_mm`: optional smoothing for outlines to reduce extremely small linear segments.
- `sampling.curve_detail_scale`: multiplier applied when tessellating curves; values above 1 tighten polygonization for smooth circles, while values below 1 speed up coarse drafts.

The sample configuration provides two profiles (`ender3_pro` and `prusa_xl`) to mirror the setups used in previous revisions.

## Usage

### GUI

The GUI is the quickest way to manage multiple SVGs, tweak their placement, change scale/rotation, and generate G-code:

```bash
/usr/bin/python3 -m svg_slicer --config config.yaml
```

Key interactions:

- Drag-and-drop SVG files (or use the “Add SVGs…” button) to queue models.
- Select a model in the list to adjust its scale (%) or rotation (°) in the sidebar; the footprint readout updates live.
- Drag models around the virtual build plate; the view auto-fits the printable area from your selected profile.
- Click “Slice” to generate the toolpaths and save the G-code to the path shown at the bottom of the sidebar.

On Wayland-based WSL environments the app automatically switches Qt to the `xcb` backend to avoid protocol errors.

### CLI

```bash
/usr/bin/python3 -m svg_slicer path/to/art.svg \
  --config config.yaml \
  --printer-profile ender3_pro \
  --output out.gcode \
  --preview-file preview.png
```

Flags:

- `--preview` opens an interactive matplotlib plot of the drawing moves.
- `--preview-file` saves the preview image instead (helpful on headless systems).
- `--printer-profile` picks a named printer profile from the configuration file.
- `--log-level` adjusts verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- `--color-mode` / `--bw-mode` let you override the profile’s default to force palette-based output or traditional black-and-white hatching on a per-run basis.

Generated G-code starts with the configured start-up sequence (home → lift → move to front-left origin), traces perimeter bands, then applies cross-hatch infill with spacing tied to grayscale brightness while respecting SVG stacking order so upper shapes mask lower ones.

When colour mode is active the slicer reports the exact pen order (least-used colour first) in the CLI/G-code comments and runs your configured pause script between colours so you only swap pens when necessary. Black-and-white mode preserves the previous brightness-to-density behaviour.

## Notes

- Filled regions and SVG strokes are both converted into hatchable toolpaths.
- Text glyphs are outlined with Matplotlib's available fonts; if a requested font is missing the fallback face is used.
- The slicer assumes the printer uses absolute coordinates and millimeters.
- Preview rendering ignores travel moves so you only see actual drawing strokes on a white background.
- For very light fills the density is clamped to `infill.min_density`, ensuring at least a faint hatch for the pen plotter.
