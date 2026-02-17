# SVG Slicer for Pen Plotter 3D Printers

SVG Slicer for Pen Plotter 3D Printers bundles a command-line tool and a PySide6 GUI that turn filled SVG artwork into pen-plotter G-code for modified 3D printers. The CLI auto-fits incoming artwork to the printable area and can emit either brightness-driven hatching or palette-based colour passes. The GUI builds on the same engine with queue management, live placement, and configuration editing so you can validate a job before exporting it.

Under the hood the slicer resolves fills, strokes, and text into geometry, applies configurable perimeter and infill strategies, and writes annotated G-code that documents colour order and motion-only plot time.

## Features

- PySide6 GUI lets you drag-and-drop multiple SVGs, rescale and rotate each model with live/editable footprint readouts, position artwork directly on the virtual build plate, zoom with the mouse wheel and pan with right-drag, duplicate selected models (`Ctrl+D`), undo placement edits (`Ctrl+Z`), save/load full arrangements as `.plot` layout files, and export the queued job to a single G-code file.
- Built-in configuration editor loads/saves YAML printer profiles including bed limits, feedrates, start/end sequences, pause scripts, and colour palettes.
- Palette-aware colour workflow maps SVG fills/strokes to the nearest configured colour, batches passes in least-used order, and inserts your pause script (default `M600`) while logging the planned colour order.
- Black-and-white mode converts fills to grayscale, driving density-scaled cross-hatch infill with perimeter glides to minimise pen lifts while thick strokes receive dedicated outline passes.
- Automatically tessellates SVG `<text>` via Matplotlib fonts, converts thick strokes into filled regions, and honours SVG z-order so upper shapes mask lower ones.
- Optional Matplotlib preview renders only drawing moves; you can display it interactively or export a PNG for headless environments.
- G-code output includes a motion-only time estimate and total line count so you can gauge run time before plotting.

## Installation

SVG Slicer targets Python 3 and relies on Shapely, svgelements, PyYAML, Matplotlib, and (for the GUI) PySide6.

- **Ubuntu / WSL packages**

  ```bash
  sudo apt-get install python3-svgelements python3-shapely python3-yaml python3-matplotlib python3-pyside6.qt6
  ```

- **pip (in a virtual environment or `--user`)**

  ```bash
  python3 -m pip install --upgrade pip
  python3 -m pip install svgelements shapely PyYAML matplotlib PySide6
  ```

PySide6 is only required when launching the GUI; the CLI can run headless.

## Configuration

Edit `config.yaml` to match your machine. Printer settings can be grouped into named profiles and switched at runtime.

- `default_printer`: optional profile name selected when `--printer-profile` is omitted.
- `printers.<profile>.name`: friendly printer label used in logs and previews.
- `printers.<profile>.bed_size_mm.width|depth`: overall bed dimensions.
- `printers.<profile>.origin_offsets_mm.x_min|x_max|y_min|y_max`: usable XY window inside the physical bed.
- `printers.<profile>.z_heights_mm.draw|travel`: Z heights for pen-down and pen-up moves; `z_lift_height_mm` optionally overrides lift distance.
- `printers.<profile>.feedrates_mm_s.draw|travel|z`: drawing, travel, and Z feedrates in mm/s.
- `printers.<profile>.start_gcode` / `end_gcode`: sequences emitted before and after plotting.
- `printers.<profile>.color_mode`: enables palette-based runs for that profile.
- `printers.<profile>.available_colors`: ordered list of `#RRGGBB` strings representing pens on hand; required when colour mode is enabled.
- `printers.<profile>.pause_gcode`: commands executed between colour batches (defaults to `["M600"]` if omitted).
- `infill.base_line_spacing_mm`, `min_density`, `max_density`, `angles_degrees`: tune cross-hatch spacing, density range, and rotation angles.
- `perimeter.thickness_mm`, `count`, `min_fill_width_mm`, `min_fill_mode`: outline line width, number of perimeter loops, the minimum feature-size threshold for infill, and how that threshold is measured (`min` for minimum local thickness, default; `max` for any dimension).
- `sampling.segment_length_tolerance_mm`, `outline_simplify_tolerance_mm`, `curve_detail_scale`: geometry sampling controls that balance fidelity against speed.
- `rendering.preview_line_width_mm`: stroke width used in Matplotlib previews.

The sample configuration contains Ender 3 Pro and Prusa XL profiles as references.

## Usage

### GUI

```bash
python3 -m svg_slicer --config config.yaml
```

- Provide `--printer-profile <name>` to open with a specific profile.
- Drop SVG files onto the build plate or use **Add SVGs…**; each file is auto-fit to the printable area once on import.
- Select a model to adjust scale (percent), rotation (degrees), and XY position; footprint width/height (mm) can be edited directly and translated into scale.
- Use the mouse wheel over the build plate to zoom and right-click drag to pan.
- Use **Edit → Undo** (`Ctrl+Z`) to revert arrangement edits (move, scale, rotation, import, duplicate, delete, clear, and layout load).
- Use **Edit → Duplicate Selected** (`Ctrl+D`) to clone one or more selected SVGs while preserving each model's current scale and rotation and offsetting position slightly for quick re-layout.
- Use **File → Save Layout As…** / **File → Load Layout…** (`.plot`) to persist and restore queued SVG placement, scale, and rotation between sessions.
- Set the destination path for the exported G-code and press **Slice** to generate toolpaths. The status bar and log window report line count, colour order (if applicable), and the estimated plot time.
- Loading layouts and applying settings show modal progress dialogs when model reload/reconfiguration work is in progress.
- Configure printer profiles, palettes, infill, and sampling values on the **Settings** tab, then apply or save back to YAML.
- On Wayland-based WSL environments the app automatically switches Qt to the `xcb` backend to avoid protocol issues.

### AI Handwriting Tab

- The GUI ships with an **AI Handwriting** tab that pipes text through the handwriting synthesis model from the companion `handwriting-data` repository.
- Clone `handwriting-data` alongside this project so the folder layout looks like:

  ```
  /path/to/workspace/
    handwriting-data/
    svg-slicer/
  ```

  Set the `SVG_SLICER_HANDWRITING_ROOT` environment variable if you keep the repository somewhere else.
- The text box live-clamps input to the model's 75 character line limit and strips unsupported characters (based on `handwriting-synthesis/drawing.py`).
- Press **Generate Preview** to launch `svg_slicer/handwriting_cli.py` in a new terminal window. The helper script checks for a Conda environment named `handwriting_tf1`, creates it with Python 3.5.2 if missing, installs the handwriting model requirements, and then runs `handwriting-synthesis/generate_from_text.py` to emit an SVG.
- Generated files and the transient line buffer live in `~/.svg_slicer/handwriting/`. When the SVG finishes rendering the preview refreshes automatically and **Save Result…** lets you copy the file anywhere (e.g. back into the Prepare tab’s queue).
- If the helper cannot find the sibling repository or cannot spawn a terminal, the status line calls it out so you can fix the setup before retrying.
- Handwriting SVGs created by the model are detected automatically when you import them in either the GUI or CLI; the slicer treats them as ordered stroke paths rather than converting them into fills, so the toolpaths match the pen strokes exactly.

### CLI

```bash
python3 -m svg_slicer.cli path/to/art.svg \
  --config config.yaml \
  --printer-profile ender3_pro \
  --output out.gcode \
  --preview-file preview.png
```

Common flags:

- `--preview` opens an interactive Matplotlib window; `--preview-file` saves the image instead.
- `--color-mode` or `--bw-mode` override the profile default for a single run.
- `--log-level` adjusts verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

The CLI reports the auto-fit scale factor, planned colour order, and motion-only time estimate in both the console and emitted G-code comments.

## Testing

Run the automated test suite with:

```bash
python3 -m pip install pytest
pytest -q
```

The current suite includes unit tests for configuration parsing, SVG parsing/fit, infill generation, G-code and CLI behavior, handwriting helpers, preview export, and GUI layout history features (undo/duplicate/layout save-load).

## Scaling and Colour Behaviour

- The CLI always fits artwork to the configured printable area and mirrors it into printer coordinates, ensuring the result lives within `origin_offsets_mm`.
- The GUI fits each SVG on import to establish a safe starting scale, but any manual scale, rotation, or placement you apply is preserved during slicing—no additional auto-scaling occurs when you press **Slice**.
- Colour mode and black-and-white mode share the same toolpath generator; palette settings live in the active printer profile and can be overridden in both CLI and GUI flows.

## Notes

- Filled regions and SVG strokes are converted into hatchable toolpaths; extremely thin strokes fall back to single tracing passes.
- Brightness mapping clamps infill density between `infill.min_density` and `infill.max_density`, enabling faint shading for light fills and solid hatching for dark regions.
- Text glyphs are outlined with Matplotlib fonts; if a requested font is unavailable the default fallback face is used.
- Generated G-code assumes absolute coordinates in millimetres.
- Preview rendering and exported toolpaths ignore travel moves so only actual drawing strokes appear.
