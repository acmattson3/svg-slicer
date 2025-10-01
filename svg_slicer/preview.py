from __future__ import annotations

from typing import Iterable, Sequence, Tuple

from .config import PrinterConfig, RenderingConfig

Point = Tuple[float, float]
Polyline = Sequence[Point]


def render_toolpaths(
    polylines: Iterable[Polyline],
    printer: PrinterConfig,
    rendering: RenderingConfig,
    output_path: str | None = None,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - matplotlib import guard
        raise RuntimeError("Matplotlib is required for preview rendering") from exc

    fig, ax = plt.subplots()
    ax.set_aspect("equal", adjustable="box")
    ax.set_facecolor("white")
    ax.set_xlim(printer.x_min, printer.x_max)
    ax.set_ylim(printer.y_min, printer.y_max)
    ax.set_title(f"Toolpath Preview - {printer.name}")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")

    # Draw the printable boundary.
    boundary_x = [printer.x_min, printer.x_max, printer.x_max, printer.x_min, printer.x_min]
    boundary_y = [printer.y_min, printer.y_min, printer.y_max, printer.y_max, printer.y_min]
    ax.plot(boundary_x, boundary_y, color="grey", linewidth=0.5, linestyle="--")

    for polyline in polylines:
        if len(polyline) < 2:
            continue
        xs, ys = zip(*polyline)
        ax.plot(xs, ys, color="black", linewidth=rendering.line_width)

    ax.set_facecolor("white")
    ax.set_axisbelow(True)
    ax.grid(True, which="both", linestyle=":", linewidth=0.3, color="#dddddd")

    if output_path:
        fig.savefig(output_path, dpi=150, facecolor="white", bbox_inches="tight")
        plt.close(fig)
    else:
        try:
            plt.show()
        except Exception:  # pragma: no cover - fallback for headless envs
            fig.savefig("gcode_preview.png", dpi=150, facecolor="white", bbox_inches="tight")
            plt.close(fig)
