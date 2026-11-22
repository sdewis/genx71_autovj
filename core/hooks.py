"""
Era-Specific GenX Radio-DJ Spoken Hook Generator for GenX71 AutoVJ.

Generates bright, upbeat 5-8 second (~14-26 word) radio-DJ opening hooks
from 30+ era-specific templates (late-70s, 80s MTV/TOTP, 90s rave/Britpop,
plus universal GenX nostalgia openers) paired with a punchy track/fact teaser.
Supports anti-repeat filtering using recently used hook IDs from SQLite.
"""

import re
import random
from typing import Any, Dict, List, Optional


_TRAILING_STOPWORDS = {
    "and", "or", "but", "the", "a", "an", "of", "in", "on", "at", "to",
    "for", "with", "by", "from", "as", "into", "that", "which", "who",
    "was", "were", "is", "are", "its", "their", "his", "her",
}


def _clean_teaser(fact: str) -> str:
    """Clean a fact for natural spoken TTS delivery without ever chopping mid-sentence."""
    text = re.sub(r"\s+", " ", (fact or "")).strip()
    if not text:
        return ""
    # If the source fact was truncated with an ellipsis, strip dangling stopwords at the end
    was_truncated = text.endswith("…") or text.endswith("...")
    text = text.replace("—", ", ").replace("–", ", ").replace("…", "").rstrip(".")
    text = re.sub(r"#(\d+)", r"number \1", text)
    text = text.replace("&", "and")

    # Keep the first complete sentence if multiple sentences were concatenated
    parts = re.split(r"(?<=[.!?])\s+", text)
    text = parts[0].strip()

    if was_truncated:
        words = text.split()
        while len(words) > 4 and words[-1].lower().strip(",;:-'\"") in _TRAILING_STOPWORDS:
            words.pop()
        text = " ".join(words).rstrip(",;:-")

    if text and not text.endswith((".", "!", "?")):
        text += "!"
    return text


def _pick_best_hook_fact(facts: Optional[List[str]]) -> str:
    """Choose the punchiest complete fact (preferring non-truncated trivia)."""
    if not facts:
        return ""
    clean = [f.strip() for f in facts if isinstance(f, str) and f.strip()]
    if not clean:
        return ""
    # Prefer complete (non-ellipsis) facts so we never read a cut-off Wikipedia snippet
    complete = [f for f in clean if not f.endswith("…") and not f.endswith("...")]
    pool = complete or clean
    best = pool[0]
    if len(best.split()) > 26:
        shorter = [f for f in pool if len(f.split()) <= 24]
        if shorter:
            best = shorter[0]
    return _clean_teaser(best)


def get_era_bucket(year: Optional[int]) -> str:
    if year is None:
        return "universal"
    y = int(year)
    if y <= 1980:
        return "late_70s"
    if y <= 1989:
        return "eighties"
    return "nineties"


# 30+ bright, upbeat GenX radio-DJ hook templates.
# Placeholders: {artist}, {title}, {year}, {peak_phrase}, {fact}
HOOK_TEMPLATES: List[Dict[str, str]] = [
    # --- Universal GenX Nostalgia Openers (12) ---
    {
        "id": "univ_where_were_you",
        "era": "universal",
        "template": "Where were you in {year} when you heard this first? {fact}",
    },
    {
        "id": "univ_bet_takes_you_back",
        "era": "universal",
        "template": "Oh, I bet this takes you right back to {year}! {fact}",
    },
    {
        "id": "univ_turn_it_up",
        "era": "universal",
        "template": "Turn this one all the way up! Here's {artist} from {year}. {fact}",
    },
    {
        "id": "univ_hands_up",
        "era": "universal",
        "template": "Hands up if you remember blasting this in {year}! {fact}",
    },
    {
        "id": "univ_instant_classic",
        "era": "universal",
        "template": "Stop scrolling, Gen X! Remember {title} from {year}? {fact}",
    },
    {
        "id": "univ_did_you_know",
        "era": "universal",
        "template": "Did you know this about {artist}'s {year} classic? {fact}",
    },
    {
        "id": "univ_soundtrack_youth",
        "era": "universal",
        "template": "Tell me this wasn't on your {year} playlist! {fact}",
    },
    {
        "id": "univ_core_memory",
        "era": "universal",
        "template": "Core memory unlocked from {year}! {fact}",
    },
    {
        "id": "univ_weekend_vibe",
        "era": "universal",
        "template": "What a tune from {year}! {artist} with {title}. {fact}",
    },
    {
        "id": "univ_still_hits",
        "era": "universal",
        "template": "Forty years later and this {year} banger still hits! {fact}",
    },
    {
        "id": "univ_guess_year",
        "era": "universal",
        "template": "Taking you straight back to {year} with {artist}! {fact}",
    },
    {
        "id": "univ_chart_flashback",
        "era": "universal",
        "template": "Gen X flashback time! {peak_phrase} {fact}",
    },

    # --- Late 70s (1978–1980): Vinyl, Punk, Disco, New Wave (6) ---
    {
        "id": "70s_drop_the_needle",
        "era": "late_70s",
        "template": "Drop the needle back to {year}! {artist} with {title}. {fact}",
    },
    {
        "id": "70s_seven_inch",
        "era": "late_70s",
        "template": "Did you own this seven inch single in {year}? {fact}",
    },
    {
        "id": "70s_vinyl_magic",
        "era": "late_70s",
        "template": "Pure {year} vinyl magic right here! {fact}",
    },
    {
        "id": "70s_top_of_the_pops",
        "era": "late_70s",
        "template": "Picture Thursday night Top of the Pops in {year}! {fact}",
    },
    {
        "id": "70s_record_shop",
        "era": "late_70s",
        "template": "Straight out of a {year} record shop! {artist} with {title}. {fact}",
    },
    {
        "id": "70s_radio_dial",
        "era": "late_70s",
        "template": "Spinning the dial all the way back to {year}! {fact}",
    },

    # --- 1980s (1981–1989): MTV, Synth, Walkman, Mixtapes (8) ---
    {
        "id": "80s_mtv_generation",
        "era": "eighties",
        "template": "Calling the MTV generation! Remember this {year} anthem? {fact}",
    },
    {
        "id": "80s_cassette_deck",
        "era": "eighties",
        "template": "Wait till this {year} synth classic kicks in! {fact}",
    },
    {
        "id": "80s_mixtape_gold",
        "era": "eighties",
        "template": "If you taped the Sunday chart show in {year}, you had this! {fact}",
    },
    {
        "id": "80s_walkman_classic",
        "era": "eighties",
        "template": "Pop this in your Walkman, it's {year}! {artist} with {title}. {fact}",
    },
    {
        "id": "80s_neon_nights",
        "era": "eighties",
        "template": "Pure eighties gold from {year}! {fact}",
    },
    {
        "id": "80s_school_disco",
        "era": "eighties",
        "template": "Remember this blasting at the {year} disco? {fact}",
    },
    {
        "id": "80s_boombox_anthem",
        "era": "eighties",
        "template": "Crank up the boombox for {artist} from {year}! {fact}",
    },
    {
        "id": "80s_totp_rewind",
        "era": "eighties",
        "template": "Rewinding straight to {year}! {peak_phrase} {fact}",
    },

    # --- 1990s (1990–1996): CD Era, Britpop, Rave, Grunge (6) ---
    {
        "id": "90s_cd_single",
        "era": "nineties",
        "template": "Who had this CD single on repeat in {year}? {fact}",
    },
    {
        "id": "90s_ninety_rewind",
        "era": "nineties",
        "template": "Pure nineties energy from {year}! {artist} with {title}. {fact}",
    },
    {
        "id": "90s_friday_night",
        "era": "nineties",
        "template": "Friday night in {year}, this one owned the room! {fact}",
    },
    {
        "id": "90s_dancefloor_memories",
        "era": "nineties",
        "template": "Oh, what a {year} anthem! Bet you still know every word. {fact}",
    },
    {
        "id": "90s_golden_era",
        "era": "nineties",
        "template": "Take me right back to {year}! {fact}",
    },
    {
        "id": "90s_chart_smasher",
        "era": "nineties",
        "template": "Massive {year} tune incoming! {peak_phrase} {fact}",
    },
]


def _format_peak_phrase(track: Dict[str, Any]) -> str:
    artist = track.get("artist", "This classic")
    title = track.get("title", "this track")
    year = track.get("year", "")
    peak = track.get("chart_peak")
    region = str(track.get("chart_region", "UK") or "UK").upper()
    region_spoken = "U K" if region == "UK" else "U S"
    if peak:
        return f"{artist}'s {title} hit {region_spoken} number {peak} in {year}!"
    return f"{artist} dropped {title} in {year}!"


def build_dj_hook_script(
    track: Dict[str, Any],
    facts: Optional[List[str]] = None,
    recent_hook_ids: Optional[List[str]] = None,
    custom_text: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> Dict[str, str]:
    """
    Build a bright, upbeat 5-8s era-specific GenX DJ spoken hook.
    Returns dict: {"hook_id": str, "era": str, "text": str}.
    """
    year = track.get("year")
    era = get_era_bucket(year)

    if custom_text and custom_text.strip():
        return {
            "hook_id": "custom",
            "era": era,
            "text": re.sub(r"\s+", " ", custom_text.strip()),
        }

    recent_set = set(recent_hook_ids or [])

    # Candidate pool: matching era templates + universal templates
    era_pool = [t for t in HOOK_TEMPLATES if t["era"] == era]
    univ_pool = [t for t in HOOK_TEMPLATES if t["era"] == "universal"]
    combined = era_pool + univ_pool

    # Filter out recently used templates to avoid repetition
    fresh = [t for t in combined if t["id"] not in recent_set]
    if not fresh:
        fresh = combined

    # Slightly prefer era-specific templates (~60% chance when available)
    r = rng if rng is not None else random
    fresh_era = [t for t in fresh if t["era"] == era]
    if fresh_era and r.random() < 0.6:
        chosen = r.choice(fresh_era)
    else:
        chosen = r.choice(fresh)

    artist = str(track.get("artist", "Unknown Artist")).strip()
    title = str(track.get("title", "Unknown Track")).strip()
    year_str = str(year) if year else "the Gen X era"
    peak_phrase = _format_peak_phrase(track)
    fact_teaser = _pick_best_hook_fact(facts)

    rendered = chosen["template"].format(
        artist=artist,
        title=title,
        year=year_str,
        peak_phrase=peak_phrase,
        fact=fact_teaser,
    )
    rendered = re.sub(r"\s+", " ", rendered).strip()
    return {
        "hook_id": chosen["id"],
        "era": chosen["era"],
        "text": rendered,
    }
