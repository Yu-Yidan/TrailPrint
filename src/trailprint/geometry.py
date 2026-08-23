"""Create a solid ribbon following a projected route."""

import math
from typing import List, Tuple

from .gpx import Point3

Face = Tuple[int, int, int]


def ribbon_mesh(points: List[Point3], width: float, base: float) -> Tuple[List[Point3], List[Face]]:
    half = width / 2
    vertices: List[Point3] = []
    for index, (x, y, z) in enumerate(points):
        before = points[max(0, index - 1)]
        after = points[min(len(points) - 1, index + 1)]
        dx, dy = after[0] - before[0], after[1] - before[1]
        length = math.hypot(dx, dy)
        if length == 0:
            raise ValueError("GPX contains consecutive duplicate track points")
        nx, ny = -dy / length * half, dx / length * half
        vertices.extend(((x + nx, y + ny, base + z), (x - nx, y - ny, base + z),
                         (x + nx, y + ny, 0.0), (x - nx, y - ny, 0.0)))

    faces: List[Face] = []
    for i in range(len(points) - 1):
        a, b = i * 4, (i + 1) * 4
        faces.extend(((a, b, a + 1), (a + 1, b, b + 1),
                      (a + 2, a + 3, b + 2), (a + 3, b + 3, b + 2),
                      (a, a + 2, b), (a + 2, b + 2, b),
                      (a + 1, b + 1, a + 3), (a + 3, b + 1, b + 3)))
    last = (len(points) - 1) * 4
    faces.extend(((0, 1, 2), (1, 3, 2),
                  (last, last + 2, last + 1), (last + 1, last + 2, last + 3)))
    return vertices, faces
