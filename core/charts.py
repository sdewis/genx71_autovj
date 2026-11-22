"""
GenX Era (1978-1996) UK Official Singles Chart & US Billboard Hot 100 Track Selector.
Includes both iconic Top 20 chart hits and unusual/cult-classic Top 21-40 wildcards.
"""

import json
import logging
import random
import re
import urllib.parse
import urllib.request
from typing import Optional, Dict, Any, List, Set, Tuple
from core.db import (
    init_db, count_tracks, add_tracks_batch,
    get_available_tracks, get_setting, get_track_by_id,
    get_max_track_batch, get_track_batches, get_existing_track_keys,
    is_blacklisted, get_active_feedback_weights,
)

logger = logging.getLogger("genx71.charts")

WIKI_HEADERS = {"User-Agent": "GenX71AutoVJ/2.0 (https://github.com/sdewis/genx71_autovj)"}
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"

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

# Extended Curated GenX Era Fallback Pool (1978-1996)
# Used to top up or supply offline batches when "Load New Items" is triggered.
GENX_EXTENDED_CATALOG = [
    # 1978-1981
    ("Buzzcocks", "Ever Fallen in Love (With Someone You Shouldn't've)", 1978, 12, "UK", 0, "Punk Rock"),
    ("The Undertones", "Teenage Kicks", 1978, 31, "UK", 1, "Punk Rock"),
    ("XTC", "Making Plans for Nigel", 1979, 17, "UK", 0, "New Wave"),
    ("The Specials", "A Message to You, Rudy", 1979, 10, "UK", 0, "2 Tone Ska"),
    ("Madness", "One Step Beyond", 1979, 7, "UK", 0, "Ska"),
    ("The Police", "Message in a Bottle", 1979, 1, "UK", 0, "New Wave"),
    ("The Pretenders", "Brass in Pocket", 1979, 1, "UK", 0, "New Wave"),
    ("Talking Heads", "Once in a Lifetime", 1980, 14, "UK", 0, "New Wave"),
    ("David Bowie", "Ashes to Ashes", 1980, 1, "UK", 0, "Art Rock"),
    ("The Beat", "Mirror in the Bathroom", 1980, 4, "UK", 0, "2 Tone Ska"),
    ("The Vapors", "Turning Japanese", 1980, 3, "UK", 0, "New Wave"),
    ("Martha and the Muffins", "Echo Beach", 1980, 10, "UK", 0, "New Wave"),
    ("Blondie", "Atomic", 1980, 1, "UK", 0, "New Wave"),
    ("Adam and the Ants", "Stand and Deliver", 1981, 1, "UK", 0, "New Romantic"),
    ("Heaven 17", "(We Don't Need This) Fascist Groove Thang", 1981, 45, "UK", 1, "Synth-Pop"),
    ("The Specials", "Ghost Town", 1981, 1, "UK", 0, "2 Tone Ska"),
    ("Kim Wilde", "Kids in America", 1981, 2, "UK", 0, "New Wave"),
    ("Orchestral Manoeuvres in the Dark", "Souvenir", 1981, 3, "UK", 0, "Synth-Pop"),
    ("Duran Duran", "Girls on Film", 1981, 5, "UK", 0, "New Romantic"),
    ("Altered Images", "Happy Birthday", 1981, 2, "UK", 0, "Post-Punk"),
    # 1982-1984
    ("ABC", "The Look of Love", 1982, 4, "UK", 0, "New Romantic"),
    ("Yazoo", "Only You", 1982, 2, "UK", 0, "Synth-Pop"),
    ("The Jam", "A Town Called Malice", 1982, 1, "UK", 0, "Mod Revival"),
    ("Culture Club", "Do You Really Want to Hurt Me", 1982, 1, "UK", 0, "New Wave"),
    ("Men at Work", "Down Under", 1982, 1, "US", 0, "New Wave"),
    ("Simple Minds", "Promised You a Miracle", 1982, 13, "UK", 0, "New Wave"),
    ("The Stranglers", "Golden Brown", 1982, 2, "UK", 0, "Art Rock"),
    ("Kajagoogoo", "Too Shy", 1983, 1, "UK", 0, "Synth-Pop"),
    ("Heaven 17", "Temptation", 1983, 2, "UK", 0, "Synth-Pop"),
    ("Kirsty MacColl", "A New England", 1984, 7, "UK", 0, "Pop Rock"),
    ("Men Without Hats", "The Safety Dance", 1983, 3, "US", 0, "Synth-Pop"),
    ("The Police", "Every Breath You Take", 1983, 1, "US", 0, "Pop Rock"),
    ("Nena", "99 Red Balloons", 1984, 1, "UK", 0, "Synth-Pop"),
    ("Cyndi Lauper", "Girls Just Want to Have Fun", 1983, 2, "US", 0, "Pop"),
    ("The Smiths", "This Charming Man", 1983, 25, "UK", 1, "Indie Rock"),
    ("Big Country", "In a Big Country", 1983, 17, "US", 0, "Post-Punk"),
    ("Howard Jones", "New Song", 1983, 3, "UK", 0, "Synth-Pop"),
    ("Nik Kershaw", "Wouldn't It Be Good", 1984, 4, "UK", 0, "Synth-Pop"),
    ("Frankie Goes to Hollywood", "Two Tribes", 1984, 1, "UK", 0, "Synth-Pop"),
    ("Thompson Twins", "Hold Me Now", 1984, 3, "US", 0, "Synth-Pop"),
    ("Ultravox", "Dancing with Tears in My Eyes", 1984, 3, "UK", 0, "Synth-Pop"),
    ("The Cars", "Drive", 1984, 3, "US", 0, "New Wave"),
    ("Alison Moyet", "Love Resurrection", 1984, 10, "UK", 0, "Synth-Pop"),
    ("Lloyd Cole and the Commotions", "Perfect Skin", 1984, 26, "UK", 1, "Indie Pop"),
    ("Everything but the Girl", "Each and Every One", 1984, 28, "UK", 1, "Sophisti-Pop"),
    # 1985-1987
    ("The Waterboys", "The Whole of the Moon", 1985, 26, "UK", 1, "Folk Rock"),
    ("Prefab Sprout", "When Love Breaks Down", 1985, 25, "UK", 1, "Sophisti-Pop"),
    ("Oingo Boingo", "Weird Science", 1985, 45, "US", 1, "New Wave"),
    ("Dead or Alive", "You Spin Me Round (Like a Record)", 1985, 1, "UK", 0, "Hi-NRG"),
    ("Fine Young Cannibals", "Johnny Come Home", 1985, 8, "UK", 0, "New Wave"),
    ("Duran Duran", "A View to a Kill", 1985, 1, "US", 0, "New Wave"),
    ("The Cult", "She Sells Sanctuary", 1985, 15, "UK", 0, "Gothic Rock"),
    ("New Order", "The Perfect Kiss", 1985, 46, "UK", 1, "Synth-Pop"),
    ("Talk Talk", "Life's What You Make It", 1986, 16, "UK", 0, "Art Rock"),
    ("The Housemartins", "Happy Hour", 1986, 3, "UK", 0, "Indie Pop"),
    ("Orchestral Manoeuvres in the Dark", "If You Leave", 1986, 4, "US", 0, "Synth-Pop"),
    ("The Communards", "Don't Leave Me This Way", 1986, 1, "UK", 0, "Hi-NRG"),
    ("Peter Gabriel", "Sledgehammer", 1986, 1, "US", 0, "Art Pop"),
    ("The Human League", "Human", 1986, 1, "US", 0, "Synth-Pop"),
    ("Erasure", "Sometimes", 1986, 2, "UK", 0, "Synth-Pop"),
    ("Cameo", "Word Up!", 1986, 3, "UK", 0, "Funk"),
    ("The Blow Monkeys", "Digging Your Scene", 1986, 12, "UK", 0, "Sophisti-Pop"),
    ("Pet Shop Boys", "It's a Sin", 1987, 1, "UK", 0, "Synth-Pop"),
    ("Fleetwood Mac", "Little Lies", 1987, 4, "US", 0, "Pop Rock"),
    ("U2", "With or Without You", 1987, 1, "US", 0, "Rock"),
    ("Suzanne Vega", "Luka", 1987, 3, "US", 0, "Folk Pop"),
    ("R.E.M.", "The One I Love", 1987, 9, "US", 0, "Alternative"),
    ("M|A|R|R|S", "Pump Up the Volume", 1987, 1, "UK", 0, "House"),
    ("T'Pau", "China in Your Hand", 1987, 1, "UK", 0, "Pop Rock"),
    ("The Primitives", "Crash", 1988, 5, "UK", 0, "Indie Pop"),
    # 1988-1991
    ("Erasure", "A Little Respect", 1988, 4, "UK", 0, "Synth-Pop"),
    ("Fairground Attraction", "Perfect", 1988, 1, "UK", 0, "Folk Pop"),
    ("Tracy Chapman", "Fast Car", 1988, 5, "UK", 0, "Folk Rock"),
    ("Transvision Vamp", "I Want Your Love", 1988, 5, "UK", 0, "Pop Punk"),
    ("Yazz", "The Only Way Is Up", 1988, 1, "UK", 0, "House"),
    ("S'Express", "Theme from S'Express", 1988, 1, "UK", 0, "Acid House"),
    ("The Church", "Under the Milky Way", 1988, 24, "US", 1, "Neo-Psychedelia"),
    ("Soul II Soul", "Back to Life", 1989, 1, "UK", 0, "R&B"),
    ("Neneh Cherry", "Buffalo Stance", 1989, 3, "UK", 0, "Hip House"),
    ("Tears for Fears", "Sowing the Seeds of Love", 1989, 2, "US", 0, "Psychedelic Pop"),
    ("Fine Young Cannibals", "She Drives Me Crazy", 1989, 1, "US", 0, "Pop Rock"),
    ("The Cure", "Lullaby", 1989, 5, "UK", 0, "Gothic Rock"),
    ("Black Box", "Ride on Time", 1989, 1, "UK", 0, "Italo House"),
    ("Pixies", "Monkey Gone to Heaven", 1989, 60, "UK", 1, "Alternative"),
    ("The La's", "There She Goes", 1990, 13, "UK", 0, "Indie Rock"),
    ("Deee-Lite", "Groove Is in the Heart", 1990, 2, "UK", 0, "Dance-Pop"),
    ("The Charlatans", "The Only One I Know", 1990, 9, "UK", 0, "Madchester"),
    ("James", "Sit Down", 1991, 2, "UK", 0, "Madchester"),
    ("Inspiral Carpets", "This Is How It Feels", 1990, 14, "UK", 0, "Madchester"),
    ("Beats International", "Dub Be Good to Me", 1990, 1, "UK", 0, "Electronic"),
    ("Primal Scream", "Loaded", 1990, 16, "UK", 0, "Indie Dance"),
    ("EMF", "Unbelievable", 1990, 1, "US", 0, "Alternative Dance"),
    ("Jesus Jones", "Right Here, Right Now", 1991, 2, "US", 0, "Alternative Dance"),
    ("Seal", "Crazy", 1991, 2, "UK", 0, "Soul/Electronic"),
    ("Blur", "There's No Other Way", 1991, 8, "UK", 0, "Madchester"),
    ("Pearl Jam", "Alive", 1991, 16, "UK", 0, "Grunge"),
    ("Electronic", "Get the Message", 1991, 8, "UK", 0, "Synth-Pop"),
    ("The Shamen", "Move Any Mountain", 1991, 4, "UK", 0, "Rave"),
    # 1992-1996
    ("The Cure", "Friday I'm in Love", 1992, 6, "UK", 0, "Alternative"),
    ("Tasmin Archer", "Sleeping Satellite", 1992, 1, "UK", 0, "Pop Rock"),
    ("Annie Lennox", "Why", 1992, 5, "UK", 0, "Synth-Pop"),
    ("Radiohead", "Creep", 1993, 7, "UK", 0, "Alternative"),
    ("The Breeders", "Cannonball", 1993, 40, "UK", 1, "Alternative"),
    ("New Order", "Regret", 1993, 4, "UK", 0, "Alternative Dance"),
    ("Pet Shop Boys", "Go West", 1993, 2, "UK", 0, "Synth-Pop"),
    ("Bjork", "Human Behaviour", 1993, 36, "UK", 1, "Electronic"),
    ("The Cranberries", "Linger", 1993, 8, "US", 0, "Alternative"),
    ("Smashing Pumpkins", "Today", 1993, 44, "UK", 1, "Alternative"),
    ("Soundgarden", "Black Hole Sun", 1994, 12, "UK", 0, "Grunge"),
    ("Green Day", "Basket Case", 1994, 7, "UK", 0, "Pop Punk"),
    ("Weezer", "Buddy Holly", 1994, 12, "UK", 0, "Alternative"),
    ("Massive Attack", "Protection", 1995, 14, "UK", 0, "Trip Hop"),
    ("Supergrass", "Alright", 1995, 2, "UK", 0, "Britpop"),
    ("Elastica", "Connection", 1994, 17, "UK", 0, "Britpop"),
    ("Oasis", "Wonderwall", 1995, 2, "UK", 0, "Britpop"),
    ("Blur", "Parklife", 1994, 10, "UK", 0, "Britpop"),
    ("Pulp", "Disco 2000", 1995, 7, "UK", 0, "Britpop"),
    ("Cast", "Alright", 1995, 13, "UK", 0, "Britpop"),
    ("The Chemical Brothers", "Setting Sun", 1996, 1, "UK", 0, "Big Beat"),
    ("Manic Street Preachers", "A Design for Life", 1996, 2, "UK", 0, "Britpop"),
    ("Ocean Colour Scene", "The Riverboat Song", 1996, 15, "UK", 0, "Britpop"),
    ("Kula Shaker", "Tattva", 1996, 4, "UK", 0, "Britpop"),
    ("Ash", "Girl from Mars", 1995, 11, "UK", 0, "Britpop"),
    ("No Doubt", "Don't Speak", 1996, 1, "UK", 0, "Pop Rock"),
    ("Fugees", "Ready or Not", 1996, 1, "UK", 0, "Hip Hop"),
]


def seed_charts_if_empty(db_path: Optional[str] = None) -> int:
    """Populate the tracks table with the curated GenX catalog if empty."""
    try:
        from core.facts import CURATED_TRACK_FACTS
    except ImportError:
        CURATED_TRACK_FACTS = {}

    init_db(db_path)
    if count_tracks(db_path) > 0:
        return 0

    batch = []
    for artist, title, year, peak, region, is_wildcard, genre in GENX_CHART_CATALOG:
        curated = CURATED_TRACK_FACTS.get((artist.lower(), title.lower()))
        batch.append({
            "artist": artist,
            "title": title,
            "year": year,
            "chart_peak": peak,
            "chart_region": region,
            "is_wildcard": is_wildcard,
            "genre": genre,
            "facts_json": json.dumps(curated) if curated else None,
            "batch_num": 1,
        })
    return add_tracks_batch(batch, batch_num=1, db_path=db_path)


def _http_get_json(url: str, params: Dict[str, Any], timeout: int = 6) -> Optional[Dict[str, Any]]:
    """Perform a lightweight GET returning parsed JSON using requests or urllib."""
    try:
        import requests  # type: ignore
        resp = requests.get(url, params=params, headers=WIKI_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except ImportError:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{url}?{qs}", headers=WIKI_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


def _clean_wiki_text(raw: str) -> str:
    """Strip common Wikipedia wikitext markup from a table cell."""
    s = raw.strip()
    # Remove <ref>...</ref> and self-closing <ref .../>
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<ref[^/>]*/\s*>", "", s, flags=re.IGNORECASE)
    # Remove HTML tags
    s = re.sub(r"<[^>]+>", "", s)
    # Remove HTML comments
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
    # Replace [[Target|Display]] -> Display, [[Display]] -> Display
    s = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    # Handle {{sortname|First|Last}} or {{nowrap|Text}}
    s = re.sub(r"\{\{\s*nowrap\s*\|([^{}]+)\}\}", r"\1", s, flags=re.IGNORECASE)
    s = re.sub(r"\{\{\s*sortname\s*\|([^|{}]+)\|([^|{}]+)(?:\|[^{}]*)?\}\}", r"\1 \2", s, flags=re.IGNORECASE)
    # Strip remaining templates {{...}}
    for _ in range(3):
        cleaned = re.sub(r"\{\{[^{}]*\}\}", "", s)
        if cleaned == s:
            break
        s = cleaned
    # Remove wiki bold/italic quotes and surrounding double quotes
    s = s.replace("'''", "").replace("''", "")
    s = s.strip().strip('"').strip("'").strip()
    # Remove trailing footnote markers
    s = re.sub(r"[\u2020\u2021#*]+$", "", s).strip()
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s


def _is_valid_track_pair(artist: str, title: str) -> bool:
    if not artist or not title:
        return False
    if len(artist) < 2 or len(title) < 2 or len(artist) > 70 or len(title) > 85:
        return False
    for bad in ("{{", "}}", "[[", "]]", "|", "http", "align=", "rowspan", "colspan", "style="):
        if bad in artist.lower() or bad in title.lower():
            return False
    return True


def _parse_billboard_yearend_wikitext(wikitext: str, year: int) -> List[Dict[str, Any]]:
    """
    Parse rows from Wikipedia's 'Billboard_Year-End_Hot_100_singles_of_{year}'.
    Typical row formats:
      | 1 || "Physical" || Olivia Newton-John
      or multi-line:
      |-
      | 1
      | "Physical"
      | Olivia Newton-John
    """
    tracks: List[Dict[str, Any]] = []
    # Split into wikitable rows
    rows = re.split(r"\n\|-[^\n]*\n", wikitext)
    for row in rows:
        lines = [ln.strip() for ln in row.splitlines() if ln.strip()]
        cells: List[str] = []
        for ln in lines:
            if ln.startswith("|}") or ln.startswith("!"):
                continue
            if ln.startswith("|"):
                body = ln[1:]
                # Split inline cells separated by ||
                for part in body.split("||"):
                    # Drop cell attributes like 'align="center" | value'
                    if "|" in part and not part.strip().startswith("[["):
                        attr_split = part.split("|", 1)
                        if "=" in attr_split[0] or "scope" in attr_split[0].lower():
                            part = attr_split[1]
                    cells.append(part.strip())
        if len(cells) < 3:
            continue
        rank_m = re.match(r"^(\d{1,3})$", _clean_wiki_text(cells[0]))
        if not rank_m:
            continue
        peak = int(rank_m.group(1))
        if peak < 1 or peak > 100:
            continue
        title = _clean_wiki_text(cells[1])
        artist = _clean_wiki_text(cells[2])
        if not _is_valid_track_pair(artist, title):
            continue
        tracks.append({
            "artist": artist,
            "title": title,
            "year": year,
            "chart_peak": peak,
            "chart_region": "US",
            "is_wildcard": 1 if peak > 20 else 0,
            "genre": "Pop/Rock",
        })
    return tracks


def _parse_uk_topten_wikitext(wikitext: str, year: int) -> List[Dict[str, Any]]:
    """
    Parse rows from Wikipedia's 'List_of_UK_top-ten_singles_in_{year}'.
    Rows commonly contain quoted song titles and artist links along with peak (1-10).
    """
    tracks: List[Dict[str, Any]] = []
    rows = re.split(r"\n\|-[^\n]*\n", wikitext)
    for row in rows:
        lines = [ln.strip() for ln in row.splitlines() if ln.strip()]
        cells: List[str] = []
        for ln in lines:
            if ln.startswith("|}") or ln.startswith("!"):
                continue
            if ln.startswith("|"):
                body = ln[1:]
                for part in body.split("||"):
                    if "|" in part and not part.strip().startswith("[["):
                        attr_split = part.split("|", 1)
                        if "=" in attr_split[0] or "scope" in attr_split[0].lower():
                            part = attr_split[1]
                    cells.append(part.strip())
        if len(cells) < 4:
            continue
        # Find the first cell that looks like a quoted single title: "..."
        title_idx = None
        for idx, c in enumerate(cells[:5]):
            if '"' in c and idx + 1 < len(cells):
                title_idx = idx
                break
        if title_idx is None:
            continue
        title = _clean_wiki_text(cells[title_idx])
        artist = _clean_wiki_text(cells[title_idx + 1])
        if not _is_valid_track_pair(artist, title):
            continue
        # Look for peak integer (1-10) in subsequent cells
        peak = None
        for c in cells[title_idx + 2:]:
            cm = re.match(r"^(\d{1,2})$", _clean_wiki_text(c))
            if cm:
                val = int(cm.group(1))
                if 1 <= val <= 10:
                    peak = val
                    break
        tracks.append({
            "artist": artist,
            "title": title,
            "year": year,
            "chart_peak": peak or 10,
            "chart_region": "UK",
            "is_wildcard": 0,
            "genre": "Pop/Rock",
        })
    return tracks


def fetch_wikipedia_chart_tracks(
    count: int = 25,
    exclude_keys: Optional[Set[Tuple[str, str]]] = None,
    timeout: int = 6,
) -> List[Dict[str, Any]]:
    """
    Fetch real 1978-1996 UK & US chart singles from the public English Wikipedia API.
    Returns up to `count` unique tracks not present in `exclude_keys`.
    """
    exclude = set(exclude_keys or set())
    years = list(range(1978, 1997))
    random.shuffle(years)

    # Query a small mix of US Billboard Year-End & UK Top-Ten pages (max 4 requests)
    page_specs = [
        (f"List_of_UK_top-ten_singles_in_{years[0]}", years[0], "UK"),
        (f"Billboard_Year-End_Hot_100_singles_of_{years[1]}", years[1], "US"),
        (f"List_of_UK_top-ten_singles_in_{years[2]}", years[2], "UK"),
        (f"Billboard_Year-End_Hot_100_singles_of_{years[3]}", years[3], "US"),
    ]

    candidates: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set(exclude)

    for wiki_title, yr, region in page_specs:
        try:
            data = _http_get_json(
                WIKI_API_URL,
                params={
                    "action": "query",
                    "titles": wiki_title,
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "format": "json",
                    "formatversion": 2,
                },
                timeout=timeout,
            )
            pages = (data or {}).get("query", {}).get("pages", [])
            if not pages or pages[0].get("missing"):
                continue
            revs = pages[0].get("revisions", [])
            if not revs:
                continue
            wikitext = revs[0].get("slots", {}).get("main", {}).get("content", "")
            if not wikitext:
                continue

            parsed = (
                _parse_uk_topten_wikitext(wikitext, yr)
                if region == "UK"
                else _parse_billboard_yearend_wikitext(wikitext, yr)
            )
            random.shuffle(parsed)
            # Take up to ~10 per page so the 25-track batch has nice variety across years & UK/US
            taken = 0
            for t in parsed:
                key = (t["artist"].lower(), t["title"].lower())
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(t)
                taken += 1
                if taken >= max(8, count // 2):
                    break
        except Exception as e:
            logger.warning(f"Wikipedia chart fetch failed for {wiki_title}: {e}")
            continue

        if len(candidates) >= count * 2:
            break

    random.shuffle(candidates)
    return candidates[:count]


def load_new_chart_batch(
    count: int = 25,
    db_path: Optional[str] = None,
    use_wikipedia: bool = True,
) -> Dict[str, Any]:
    """
    Load a new batch of `count` unique GenX (1978-1996) chart tracks into SQLite
    under a brand-new batch_num (max_batch + 1), pushing existing pages back by 1.
    Uses Wikipedia API first, falling back / topping up from GENX_EXTENDED_CATALOG.
    """
    seed_charts_if_empty(db_path)

    count = max(1, min(int(count), 50))
    existing_keys = get_existing_track_keys(db_path)

    selected: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set(existing_keys)
    wiki_added = 0
    catalog_added = 0

    # 1. Try live Wikipedia 1978-1996 charts
    if use_wikipedia:
        wiki_tracks = fetch_wikipedia_chart_tracks(count=count, exclude_keys=seen)
        for t in wiki_tracks:
            key = (t["artist"].lower(), t["title"].lower())
            if key in seen or is_blacklisted(t["artist"], t["title"], db_path=db_path):
                continue
            seen.add(key)
            selected.append(t)
            wiki_added += 1
            if len(selected) >= count:
                break

    # 2. Top up / fallback from expanded curated catalog
    if len(selected) < count:
        pool = list(GENX_EXTENDED_CATALOG)
        random.shuffle(pool)
        for artist, title, year, peak, region, is_wildcard, genre in pool:
            key = (artist.lower(), title.lower())
            if key in seen or is_blacklisted(artist, title, db_path=db_path):
                continue
            seen.add(key)
            selected.append({
                "artist": artist,
                "title": title,
                "year": year,
                "chart_peak": peak,
                "chart_region": region,
                "is_wildcard": is_wildcard,
                "genre": genre,
            })
            catalog_added += 1
            if len(selected) >= count:
                break

    if not selected:
        batches = get_track_batches(db_path)
        return {
            "ok": True,
            "added": 0,
            "batch_num": get_max_track_batch(db_path),
            "source": "none",
            "total_pages": max(1, len(batches)),
            "message": "No new unique tracks available right now.",
        }

    new_batch = get_max_track_batch(db_path) + 1
    inserted = add_tracks_batch(selected, batch_num=new_batch, db_path=db_path)
    batches = get_track_batches(db_path)

    if wiki_added > 0 and catalog_added > 0:
        source = "hybrid"
    elif wiki_added > 0:
        source = "wikipedia"
    else:
        source = "catalog"

    return {
        "ok": True,
        "added": inserted,
        "batch_num": new_batch,
        "source": source,
        "total_pages": max(1, len(batches)),
        "message": f"Loaded {inserted} new tracks into Page 1 ({source}).",
    }


def compute_track_feedback_weight(
    track: Dict[str, Any],
    feedback: Optional[Dict[str, Any]],
    strength: float = 0.5,
) -> float:
    """
    Compute a positive selection weight for a candidate track using the latest
    daily analytics feedback weights (genre, decade, chart region, avoid list).
    """
    if not feedback or strength <= 0:
        return 1.0

    s = max(0.0, min(1.0, float(strength)))
    raw_weight = 1.0

    genre = (track.get("genre") or "").strip().lower()
    pref_genres = {
        str(k).strip().lower(): float(v)
        for k, v in (feedback.get("preferred_genres") or {}).items()
    }
    if genre and genre in pref_genres:
        raw_weight *= pref_genres[genre]

    avoid = {str(g).strip().lower() for g in (feedback.get("avoid_genres") or []) if g}
    if genre and genre in avoid:
        raw_weight *= 0.6

    year = track.get("year")
    if year:
        decade = f"{(int(year) // 10) * 10}s"
        pref_decades = {
            str(k).strip().lower(): float(v)
            for k, v in (feedback.get("preferred_decades") or {}).items()
        }
        if decade.lower() in pref_decades:
            raw_weight *= pref_decades[decade.lower()]

    region = (track.get("chart_region") or "").strip().lower()
    pref_regions = {
        str(k).strip().lower(): float(v)
        for k, v in (feedback.get("preferred_regions") or {}).items()
    }
    if region and region in pref_regions:
        raw_weight *= pref_regions[region]

    # Blend toward neutral 1.0 based on configured feedback strength and clamp for diversity
    blended = 1.0 + (raw_weight - 1.0) * s
    return round(max(0.25, min(3.0, blended)), 4)


def select_next_track(track_id: Optional[int] = None, force_wildcard: Optional[bool] = None,
                      db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Select the next GenX track to render & post.
    - Respects permanent deduplication and blacklist rules.
    - Uses configurable wildcard_chance (default 25%) to pick unusual Top 21-40 gems.
    - When analytics_feedback_enabled is 'true', biases track selection using the
      active daily AI analytics feedback weights while preserving catalog diversity.
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

    feedback_enabled = get_setting("analytics_feedback_enabled", "true", db_path=db_path) == "true"
    if feedback_enabled:
        feedback = get_active_feedback_weights(db_path=db_path)
        if feedback:
            try:
                strength = float(get_setting("analytics_feedback_strength", "0.5", db_path=db_path) or "0.5")
            except ValueError:
                strength = 0.5
            weights = [compute_track_feedback_weight(c, feedback, strength=strength) for c in candidates]
            if any(w != 1.0 for w in weights):
                return random.choices(candidates, weights=weights, k=1)[0]

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
