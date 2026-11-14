"""
GenX Era (1978-1996) UK Official Singles Chart & US Billboard Hot 100 Track Selector.
Includes both iconic Top 20 chart hits and unusual/cult-classic Top 21-40 wildcards.
"""

import random
from typing import Optional, Dict, Any, List
from core.db import (
    init_db, count_tracks, add_tracks_batch,
    get_available_tracks, get_setting, get_track_by_id
)

# Curated GenX Era Chart Catalog (1978-1996)
# Format: (artist, title, year, chart_peak, chart_region, is_wildcard, genre)
GENX_CHART_CATALOG = [
    # --- Top 20 Classics (is_wildcard=0) ---
    ("Kate Bush", "Wuthering Heights", 1978, 1, "UK", 0, "Art Pop"),
    ("Blondie", "Heart of Glass", 1979, 1, "UK", 0, "New Wave"),
    ("The Buggles", "Video Killed the Radio Star", 1979, 1, "UK", 0, "Synth-Pop"),
    ("Gary Numan", "Cars", 1979, 1, "UK", 0, "Synth-Pop"),
    ("The Clash", "London Calling", 1979, 11, "UK", 0, "Punk Rock"),
    ("Joy Division", "Love Will Tear Us Apart", 1980, 13, "UK", 0, "Post-Punk"),
    ("The Jam", "Going Underground", 1980, 1, "UK", 0, "Mod Revival"),
    ("Soft Cell", "Tainted Love", 1981, 1, "UK", 0, "Synth-Pop"),
    ("The Human League", "Don't You Want Me", 1981, 1, "UK", 0, "Synth-Pop"),
    ("Orchestral Manoeuvres in the Dark", "Enola Gay", 1980, 8, "UK", 0, "Synth-Pop"),
    ("Depeche Mode", "Just Can't Get Enough", 1981, 8, "UK", 0, "Synth-Pop"),
    ("Ultravox", "Vienna", 1981, 2, "UK", 0, "New Wave"),
    ("Duran Duran", "Rio", 1982, 9, "UK", 0, "New Romantic"),
    ("A Flock of Seagulls", "I Ran (So Far Away)", 1982, 9, "US", 0, "New Wave"),
    ("Dexys Midnight Runners", "Come On Eileen", 1982, 1, "UK", 0, "Pop/Folk"),
    ("Tears for Fears", "Mad World", 1982, 3, "UK", 0, "Synth-Pop"),
    ("New Order", "Blue Monday", 1983, 9, "UK", 0, "Synth-Pop"),
    ("Eurythmics", "Sweet Dreams (Are Made of This)", 1983, 2, "UK", 0, "Synth-Pop"),
    ("Spandau Ballet", "True", 1983, 1, "UK", 0, "New Romantic"),
    ("David Bowie", "Let's Dance", 1983, 1, "UK", 0, "Pop Rock"),
    ("Frankie Goes to Hollywood", "Relax", 1983, 1, "UK", 0, "Synth-Pop"),
    ("Bronski Beat", "Smalltown Boy", 1984, 3, "UK", 0, "Synth-Pop"),
    ("Prince", "When Doves Cry", 1984, 1, "US", 0, "Pop/Funk"),
    ("Talk Talk", "It's My Life", 1984, 13, "UK", 0, "Synth-Pop"),
    ("Alphaville", "Forever Young", 1984, 1, "EU", 0, "Synth-Pop"),
    ("a-ha", "Take On Me", 1985, 1, "US", 0, "Synth-Pop"),
    ("Tears for Fears", "Everybody Wants to Rule the World", 1985, 1, "US", 0, "New Wave"),
    ("Simple Minds", "Don't You (Forget About Me)", 1985, 1, "US", 0, "New Wave"),
    ("The Cure", "In Between Days", 1985, 15, "UK", 0, "Post-Punk"),
    ("Pet Shop Boys", "West End Girls", 1985, 1, "UK", 0, "Synth-Pop"),
    ("Kate Bush", "Running Up That Hill", 1985, 3, "UK", 0, "Art Pop"),
    ("The Smiths", "There Is a Light That Never Goes Out", 1986, 15, "UK", 0, "Indie Rock"),
    ("Berlin", "Take My Breath Away", 1986, 1, "UK", 0, "Synth-Pop"),
    ("Crowded House", "Don't Dream It's Over", 1986, 2, "US", 0, "Pop Rock"),
    ("INXS", "Need You Tonight", 1987, 1, "US", 0, "Rock/Funk"),
    ("The Cure", "Just Like Heaven", 1987, 19, "UK", 0, "Alternative"),
    ("Depeche Mode", "Never Let Me Down Again", 1987, 22, "UK", 0, "Synth-Pop"),
    ("New Order", "True Faith", 1987, 4, "UK", 0, "Synth-Pop"),
    ("Pixies", "Where Is My Mind?", 1988, 10, "US", 0, "Alternative"),
    ("The Stone Roses", "Fools Gold", 1989, 8, "UK", 0, "Madchester"),
    ("The B-52's", "Love Shack", 1989, 2, "UK", 0, "New Wave"),
    ("Depeche Mode", "Enjoy the Silence", 1990, 6, "UK", 0, "Synth-Pop"),
    ("Sinead O'Connor", "Nothing Compares 2 U", 1990, 1, "UK", 0, "Pop"),
    ("Happy Mondays", "Step On", 1990, 5, "UK", 0, "Madchester"),
    ("Massive Attack", "Unfinished Sympathy", 1991, 13, "UK", 0, "Trip Hop"),
    ("Nirvana", "Smells Like Teen Spirit", 1991, 6, "US", 0, "Grunge"),
    ("R.E.M.", "Losing My Religion", 1991, 4, "US", 0, "Alternative"),
    ("Electronic", "Getting Away with It", 1989, 12, "UK", 0, "Synth-Pop"),
    ("Snap!", "Rhythm Is a Dancer", 1992, 1, "UK", 0, "Eurodance"),
    ("Suede", "Animal Nitrate", 1993, 7, "UK", 0, "Britpop"),
    ("Blur", "Girls & Boys", 1994, 5, "UK", 0, "Britpop"),
    ("Oasis", "Live Forever", 1994, 10, "UK", 0, "Britpop"),
    ("Portishead", "Glory Box", 1994, 13, "UK", 0, "Trip Hop"),
    ("Pulp", "Common People", 1995, 2, "UK", 0, "Britpop"),
    ("Garbage", "Stupid Girl", 1996, 4, "UK", 0, "Alternative"),
    ("Underworld", "Born Slippy .NUXX", 1996, 2, "UK", 0, "Electronic"),
    ("The Prodigy", "Firestarter", 1996, 1, "UK", 0, "Big Beat"),

    # --- Unusual / Top 21-40 Wildcards & Cult Classics (is_wildcard=1) ---
    ("The Normal", "Warm Leatherette", 1978, 38, "UK", 1, "Minimal Synth"),
    ("Magazine", "Shot by Both Sides", 1978, 41, "UK", 1, "Post-Punk"),
    ("Wire", "Outdoor Miner", 1979, 51, "UK", 1, "Post-Punk"),
    ("Tubeway Army", "Down in the Park", 1979, 34, "UK", 1, "Synth-Punk"),
    ("Fad Gadget", "Back to Nature", 1979, 39, "UK", 1, "Industrial"),
    ("Visage", "Fade to Grey", 1980, 8, "UK", 1, "New Romantic"),
    ("John Foxx", "Underpass", 1980, 31, "UK", 1, "Synth-Pop"),
    ("A Certain Ratio", "Shack Up", 1980, 35, "UK", 1, "Post-Punk Funk"),
    ("Japan", "Ghosts", 1982, 5, "UK", 1, "Art Pop"),
    ("Landscape", "Einstein a Go-Go", 1981, 5, "UK", 1, "Synth-Pop"),
    ("Bauhaus", "Bela Lugosi's Dead", 1979, 36, "UK", 1, "Gothic Rock"),
    ("The Associates", "Party Fears Two", 1982, 9, "UK", 1, "New Wave"),
    ("Blancmange", "Living on the Ceiling", 1982, 7, "UK", 1, "Synth-Pop"),
    ("Thomas Dolby", "She Blinded Me with Science", 1982, 49, "UK", 1, "Synth-Pop"),
    ("Yazoo", "Situation", 1982, 33, "UK", 1, "Synth-Pop"),
    ("The Chameleons", "Up the Down Escalator", 1983, 37, "UK", 1, "Post-Punk"),
    ("Cocteau Twins", "Pearly-Dewdrops' Drops", 1984, 29, "UK", 1, "Dream Pop"),
    ("The Psychedelic Furs", "The Ghost in You", 1984, 68, "UK", 1, "New Wave"),
    ("Echo & the Bunnymen", "The Killing Moon", 1984, 9, "UK", 1, "Post-Punk"),
    ("Siouxsie and the Banshees", "Cities in Dust", 1985, 21, "UK", 1, "Post-Punk"),
    ("Propaganda", "Duel", 1985, 21, "UK", 1, "Synth-Pop"),
    ("The Sisters of Mercy", "This Corrosion", 1987, 7, "UK", 1, "Gothic Rock"),
    ("The Jesus and Mary Chain", "April Skies", 1987, 8, "UK", 1, "Noise Pop"),
    ("My Bloody Valentine", "Soon", 1990, 41, "UK", 1, "Shoegaze"),
    ("Ride", "Vapour Trail", 1991, 33, "UK", 1, "Shoegaze"),
    ("Slowdive", "Alison", 1993, 35, "UK", 1, "Shoegaze"),
    ("Saint Etienne", "Only Love Can Break Your Heart", 1990, 39, "UK", 1, "Indie Dance"),
    ("808 State", "Pacific State", 1989, 10, "UK", 1, "Acid House"),
    ("The KLF", "3 a.m. Eternal", 1991, 1, "UK", 1, "Stadium House"),
    ("Orbital", "Chime", 1990, 17, "UK", 1, "Techno"),
    ("Aphex Twin", "Didgeridoo", 1992, 55, "UK", 1, "IDM"),
    ("Lush", "Ladykillers", 1996, 22, "UK", 1, "Britpop"),
    ("Sleeper", "Inbetweener", 1995, 16, "UK", 1, "Britpop"),
    ("Shed Seven", "Going for Gold", 1996, 8, "UK", 1, "Britpop"),
]


def seed_charts_if_empty(db_path: Optional[str] = None) -> int:
    """Populate the tracks table with the curated GenX catalog if empty."""
    init_db(db_path)
    if count_tracks(db_path) > 0:
        return 0

    batch = []
    for artist, title, year, peak, region, is_wildcard, genre in GENX_CHART_CATALOG:
        batch.append({
            "artist": artist,
            "title": title,
            "year": year,
            "chart_peak": peak,
            "chart_region": region,
            "is_wildcard": is_wildcard,
            "genre": genre
        })
    return add_tracks_batch(batch, db_path=db_path)


def select_next_track(track_id: Optional[int] = None, force_wildcard: Optional[bool] = None,
                      db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Select the next GenX track to render & post.
    - Respects permanent deduplication and blacklist rules.
    - Uses configurable wildcard_chance (default 25%) to pick unusual Top 21-40 gems.
    """
    seed_charts_if_empty(db_path)

    if track_id is not None:
        return get_track_by_id(track_id, db_path=db_path)

    wildcard_chance = int(get_setting("wildcard_chance", "25", db_path=db_path) or "25")

    if force_wildcard is None:
        pick_wildcard = random.randint(1, 100) <= wildcard_chance
    else:
        pick_wildcard = force_wildcard

    candidates = get_available_tracks(is_wildcard=pick_wildcard, db_path=db_path)

    # Fallback to any available track if the chosen bucket is exhausted
    if not candidates:
        candidates = get_available_tracks(is_wildcard=None, db_path=db_path)

    if not candidates:
        return None

    return random.choice(candidates)


def format_chart_badge(track: Dict[str, Any]) -> str:
    """Format a sleek chart badge string, e.g. 'UK #9 • 1983' or 'WILDCARD #37 • 1983'."""
    peak = track.get("chart_peak")
    region = track.get("chart_region", "UK")
    year = track.get("year", "")
    is_wc = bool(track.get("is_wildcard", 0))

    if peak:
        prefix = f"{region} #{peak}"
        if is_wc and peak > 20:
            prefix = f"DEEP CUT • {region} #{peak}"
        return f"{prefix} • {year}"
    return f"GENX CLASSIC • {year}"
