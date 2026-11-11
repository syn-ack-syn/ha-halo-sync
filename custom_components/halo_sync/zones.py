"""Which Ambilight pixels each light follows ("zone modes").

The TV reports a ring of edge pixels (/ambilight/measured). Pixels are numbered clockwise from the
bottom-left corner: left (bottom -> top), top (left -> right), right (top -> bottom), bottom
(right -> left). On this TV the ring is 3 / 7 / 3 / 8 pixels.
"""

from __future__ import annotations

import math

SIDES = ("left", "top", "right", "bottom")
NATIVE_SIZES = (3, 7, 3, 8)

# The TV's own Ambilight+Hue splits the ring into 10 zones. These are the ring indices each zone
# averages, obtained by emulating libhwambilight's amb_hue_calculateFromHaloPixels (ARM) for the
# 3/7/3/8 ring. Zone 15 and 18 are shared on purpose (the firmware overlaps neighbouring zones).
NATIVE_ZONES: dict[int, tuple[int, ...]] = {
    0: (18, 19, 20),
    1: (0, 1),
    2: (2,),
    3: (3, 4),
    4: (5, 6, 7),
    5: (8, 9),
    6: (10,),
    7: (11, 12),
    8: (13, 14, 15),
    9: (15, 16, 17, 18),
}
NATIVE_ZONE_NAMES = {
    0: "Bottom left",
    1: "Left lower",
    2: "Left upper",
    3: "Top left",
    4: "Top middle",
    5: "Top right",
    6: "Right upper",
    7: "Right lower",
    8: "Bottom right",
    9: "Bottom middle",
}


def pixel_pos(side: str, i: int, n: int) -> tuple[float, float]:
    """Position (x, z) of a ring pixel on the unit screen square (-1..1)."""
    f = (2 * i + 1) / n
    return {"left": (-1, -1 + f), "top": (-1 + f, 1), "right": (1, 1 - f), "bottom": (1 - f, -1)}[side]


def ring_sizes(layer: dict) -> tuple[int, ...]:
    return tuple(len(layer.get(side) or {}) for side in SIDES)


def ring_colors(layer: dict) -> list[tuple[int, int, int]]:
    """RGB (0..255) of every ring pixel, in ring order."""
    out = []
    for side in SIDES:
        px = layer.get(side) or {}
        for i in range(len(px)):
            c = px[str(i)]
            out.append((c["r"], c["g"], c["b"]))
    return out


def ring_layout(sizes: tuple[int, ...]) -> list[tuple[str, int, float, float]]:
    """(side, index within side, x, z) for every ring pixel, in ring order."""
    return [(side, i, *pixel_pos(side, i, n)) for side, n in zip(SIDES, sizes) for i in range(n)]


def nearest_side(x: float, z: float) -> str:
    """Screen side closest to a position (distance to the side's segment)."""
    ox = max(0.0, abs(x) - 1)
    oz = max(0.0, abs(z) - 1)
    d = {
        "left": math.hypot(x + 1, oz),
        "right": math.hypot(x - 1, oz),
        "top": math.hypot(ox, z - 1),
        "bottom": math.hypot(ox, z + 1),
    }
    return min(d, key=d.get)


def native_zone_centers() -> dict[int, tuple[float, float]]:
    layout = ring_layout(NATIVE_SIZES)
    out = {}
    for zone, idx in NATIVE_ZONES.items():
        out[zone] = (sum(layout[i][2] for i in idx) / len(idx), sum(layout[i][3] for i in idx) / len(idx))
    return out


_CENTERS = native_zone_centers()


def default_native_zone(x: float, z: float) -> int:
    """TV-native zone whose centre is closest to a position."""
    return min(_CENTERS, key=lambda k: (_CENTERS[k][0] - x) ** 2 + (_CENTERS[k][1] - z) ** 2)


def _normalise(w: list[float]) -> list[float]:
    s = sum(w)
    return [v / s for v in w] if s > 0 else w


def weights(
    mode: str,
    sizes: tuple[int, ...],
    x: float,
    z: float,
    spread: float = 0.8,
    count: int = 3,
    native_zone: int | None = None,
) -> list[float]:
    """Weight of each ring pixel (sums to 1) for a light at (x, z)."""
    layout = ring_layout(sizes)
    n = len(layout)
    if mode == "native" and sizes == NATIVE_SIZES:
        zone = native_zone if native_zone in NATIVE_ZONES else default_native_zone(x, z)
        w = [0.0] * n
        for i in NATIVE_ZONES[zone]:
            w[i] = 1.0
        return _normalise(w)
    if mode in ("nearest", "some", "side", "native"):  # "native" on a differently sized ring degrades to "nearest"
        side = nearest_side(x, z)
        start = sum(sizes[: SIDES.index(side)])
        idx = list(range(start, start + sizes[SIDES.index(side)]))
        if not idx:
            return weights("smooth", sizes, x, z, spread)
        coord = (lambda p: p[3]) if side in ("left", "right") else (lambda p: p[2])
        pos = x if side in ("top", "bottom") else z
        order = sorted(idx, key=lambda i: abs(coord(layout[i]) - pos))
        if mode == "side":
            chosen = idx
        elif mode == "some":
            # The k pixels nearest to the light along the side (a contiguous run).
            chosen = order[: max(1, min(int(count), len(idx)))]
        else:
            chosen = order[:1]
        w = [0.0] * n
        for i in chosen:
            w[i] = 1.0
        return _normalise(w)
    # "smooth": gaussian falloff with the distance on the screen edge
    sigma2 = 2 * spread**2
    return _normalise([math.exp(-((px - x) ** 2 + (pz - z) ** 2) / sigma2) for _, _, px, pz in layout])


class ZoneSampler:
    """Caches pixel weights per light; they only change with settings or light positions."""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._w: list[list[float]] = []

    def sample(
        self,
        layer: dict,
        positions: list[tuple[float, float]],
        mode: str,
        spread: float,
        count: int,
        zones: list[int | None],
        saturation: float,
    ) -> list[list[float]]:
        """Colour (0..1 RGB) every light should follow."""
        sizes = ring_sizes(layer)
        key = (sizes, tuple(positions), mode, round(spread, 3), int(count), tuple(zones))
        if key != self._key:
            self._w = [
                weights(mode, sizes, x, z, spread, count, zones[j] if j < len(zones) else None)
                for j, (x, z) in enumerate(positions)
            ]
            self._key = key
        colors = ring_colors(layer)
        out = []
        for w in self._w:
            rgb = [0.0, 0.0, 0.0]
            for wi, c in zip(w, colors):
                if wi:
                    rgb[0] += wi * c[0]
                    rgb[1] += wi * c[1]
                    rgb[2] += wi * c[2]
            rgb = [v / 255 for v in rgb]
            if saturation != 1.0:
                m = sum(rgb) / 3
                rgb = [min(1.0, max(0.0, m + (v - m) * saturation)) for v in rgb]
            out.append(rgb)
        return out
