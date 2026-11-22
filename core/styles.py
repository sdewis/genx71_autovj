"""
Visual Style Pack Registry & Era-Weighted Auto-Rotation for GenX71 AutoVJ.

6 deterministic, code-rendered visual styles inspired by lemo-opuscar,
bang-motion, and procedural shader craft (0 AI tokens at runtime):
  - hifi:     Hi-Fi Audiophile (3D Vinyl + Tonearm pre-1988 / Glossy 3D CD 1988+)
  - vhs:      VHS / MTV Broadcast (CRT scanlines, tracking intro, MTV lower-third)
  - neon:     Neon Tubes (80s Synthwave horizon grid, glowing neon bloom frame)
  - riso:     Risograph Zine Poster (tactile paper grain, halftone dots, taped print)
  - kinetic:  Kinetic Color-Block (bold artwork palette fields, giant drifting type)
  - cassette: Cassette / Boombox Deck (dual spinning reels, analog amber VU meters)
"""

import random
from typing import Dict, List, Optional, Tuple, Any

STYLE_PACKS: List[Dict[str, str]] = [
    {
        "id": "hifi",
        "name": "Hi-Fi Audiophile",
        "subtitle": "3D Vinyl + Tonearm / Glossy CD Case",
    },
    {
        "id": "vhs",
        "name": "VHS / MTV Broadcast",
        "subtitle": "CRT Scanlines, Tracking Lock & MTV Lower-Third",
    },
    {
        "id": "neon",
        "name": "Neon Tubes (80s Synth)",
        "subtitle": "Synthwave Horizon Grid & Glowing Neon Bloom",
    },
    {
        "id": "riso",
        "name": "Risograph Zine Poster",
        "subtitle": "Halftone Print Texture, Paper Grain & Ink Stamps",
    },
    {
        "id": "kinetic",
        "name": "Kinetic Color-Block",
        "subtitle": "Bold Palette Fields & Giant Cropped Typography",
    },
    {
        "id": "cassette",
        "name": "Cassette / Boombox Deck",
        "subtitle": "Spinning Tape Reels & Analog Amber VU Meters",
    },
]

VALID_STYLE_IDS = {s["id"] for s in STYLE_PACKS}

# Map legacy template identifiers to (style_id, optional media_variant)
LEGACY_TEMPLATE_MAP: Dict[str, Tuple[str, Optional[str]]] = {
    "vinyl": ("hifi", "vinyl"),
    "cd": ("hifi", "cd"),
    "retro": ("hifi", "card"),
}

# Era-weighted probability tables across 1978-1996
_ERA_WEIGHTS: List[Tuple[int, int, Dict[str, float]]] = [
    # Late 70s / Early 80s (1978-1982): Vinyl, Zine/Punk/New Wave, Cassette, early MTV
    (1978, 1982, {"hifi": 0.30, "riso": 0.24, "cassette": 0.22, "vhs": 0.12, "neon": 0.08, "kinetic": 0.04}),
    # Peak 80s (1983-1987): Neon Synth, Cassette Walkman, MTV VHS, Hi-Fi
    (1983, 1987, {"neon": 0.28, "cassette": 0.24, "vhs": 0.22, "hifi": 0.14, "kinetic": 0.06, "riso": 0.06}),
    # Late 80s / Early 90s (1988-1992): MTV Broadcast, Kinetic Poster, CD Hi-Fi, Cassette, Neon
    (1988, 1992, {"vhs": 0.24, "kinetic": 0.24, "hifi": 0.22, "cassette": 0.12, "neon": 0.10, "riso": 0.08}),
    # Mid 90s (1993-1996): CD Hi-Fi, Kinetic Poster, Zine/Britpop/Grunge, VHS Broadcast
    (1993, 1996, {"hifi": 0.28, "kinetic": 0.28, "riso": 0.22, "vhs": 0.14, "cassette": 0.04, "neon": 0.04}),
]

_DEFAULT_WEIGHTS: Dict[str, float] = {
    "hifi": 0.20,
    "vhs": 0.18,
    "neon": 0.16,
    "riso": 0.15,
    "kinetic": 0.16,
    "cassette": 0.15,
}


def resolve_style_and_variant(template: Optional[str], year: Optional[int] = None) -> Tuple[str, Optional[str]]:
    """
    Resolve a template/style name into (style_id, media_variant).
    Legacy names ('vinyl', 'cd', 'retro') map cleanly onto 'hifi' with their
    matching physical media variant.
    """
    if not template:
        return ("hifi", "cd" if (year is not None and year >= 1988) else "vinyl")

    key = template.strip().lower()
    if key in LEGACY_TEMPLATE_MAP:
        return LEGACY_TEMPLATE_MAP[key]
    if key in VALID_STYLE_IDS:
        variant = None
        if key == "hifi":
            variant = "cd" if (year is not None and year >= 1988) else "vinyl"
        return (key, variant)
    return ("hifi", "cd" if (year is not None and year >= 1988) else "vinyl")


def _weights_for_year(year: Optional[int]) -> Dict[str, float]:
    if year is None:
        return dict(_DEFAULT_WEIGHTS)
    for lo, hi, weights in _ERA_WEIGHTS:
        if lo <= year <= hi:
            return dict(weights)
    if year < 1978:
        return dict(_ERA_WEIGHTS[0][2])
    return dict(_ERA_WEIGHTS[-1][2])


def select_style_pack(
    year: Optional[int],
    recent_styles: Optional[List[str]] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """
    Era-Weighted Auto-Rotate style picker.
    Favors styles authentic to the track's release era while excluding the
    last 2 recently used styles so the feed stays visually fresh.
    """
    weights = _weights_for_year(year)

    # Normalize recent styles (mapping any legacy names to their style_id)
    excluded = set()
    if recent_styles:
        for raw in recent_styles[:2]:
            if not raw:
                continue
            sid, _ = resolve_style_and_variant(raw, year)
            excluded.add(sid)

    candidates = [(sid, w) for sid, w in weights.items() if sid not in excluded and w > 0]
    if not candidates:
        candidates = list(weights.items())

    total = sum(w for _, w in candidates)
    gen = rng if rng is not None else random
    pick = gen.random() * total
    acc = 0.0
    for sid, w in candidates:
        acc += w
        if pick <= acc:
            return sid
    return candidates[-1][0]


def get_style_metadata() -> List[Dict[str, Any]]:
    return [dict(s) for s in STYLE_PACKS]
