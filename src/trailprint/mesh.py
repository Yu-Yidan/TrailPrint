"""ASCII STL output."""

import math
from pathlib import Path
from typing import Iterable, Tuple

from .geometry import Face
from .gpx import Point3


def _normal(a: Point3, b: Point3, c: Point3) -> Point3:
    ab, ac = tuple(b[i] - a[i] for i in range(3)), tuple(c[i] - a[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    length = math.sqrt(sum(v * v for v in cross))
    return tuple(v / length for v in cross) if length else (0.0, 0.0, 0.0)


def write_stl(path: Path, vertices: Iterable[Point3], faces: Iterable[Face], name: str) -> None:
    verts = list(vertices)
    lines = [f"solid {name}"]
    for face in faces:
        triangle = [verts[i] for i in face]
        normal = _normal(*triangle)
        lines.append("  facet normal " + " ".join(f"{v:.6g}" for v in normal))
        lines.append("    outer loop")
        lines.extend("      vertex " + " ".join(f"{v:.6g}" for v in point) for point in triangle)
        lines.extend(("    endloop", "  endfacet"))
    lines.append(f"endsolid {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
