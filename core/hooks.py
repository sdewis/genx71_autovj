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


# Hook length definitions (~5-8s spoken DJ intro, safely under XTTS-v2's 250-char English limit)
HOOK_TARGET_MIN_WORDS = 14
HOOK_MAX_WORDS = 26
HOOK_MAX_CHARS = 180

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "ft",
    "no", "vol", "vs", "feat", "etc", "eg", "ie", "am", "pm",
    "jan", "feb", "mar", "apr", "aug", "sep", "sept", "oct", "nov", "dec",
}
_DOTTED_INITIAL_RE = re.compile(r"^(?:[A-Za-z]\.)+$")
_SENTENCE_END_RE = re.compile(r"[.!?]+(?:\s+|$)")
_FACT_REQUIRED_TEMPLATE_IDS = {"univ_did_you_know"}


def split_sentences(text: str) -> List[str]:
    """
    Split text into complete sentences without breaking on initials or common
    abbreviations (e.g. T.S. Eliot, J.G. Ballard, P.M. Dawn, Richard D. James, Dr. Dre).
    """
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if not normalized:
        return []

    sentences: List[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(normalized):
        punct = match.group(0).strip()
        end_pos = match.end()

        if punct == ".":
            prefix = normalized[start : match.start() + 1]
            last_token = prefix.rsplit(" ", 1)[-1].lstrip("\"'([")
            bare_token = last_token.rstrip(".").lower()
            if _DOTTED_INITIAL_RE.match(last_token) or bare_token in _ABBREVIATIONS:
                continue

        chunk = normalized[start:end_pos].strip()
        if chunk:
            sentences.append(chunk)
        start = end_pos

    tail = normalized[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def fits_hook_length(
    text: str,
    max_words: int = HOOK_MAX_WORDS,
    max_chars: int = HOOK_MAX_CHARS,
) -> bool:
    """Return True if text fits within the configured hook word and character limits."""
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if not normalized:
        return True
    return len(normalized.split()) <= max_words and len(normalized) <= max_chars


def fit_hook_text_to_limits(
    text: str,
    max_words: int = HOOK_MAX_WORDS,
    max_chars: int = HOOK_MAX_CHARS,
) -> str:
    """
    Check if hook text fits within the hook length definitions.
    If not, reduce the number of sentences from the end until the remaining
    complete sentence(s) fit while still making grammatical sense.
    """
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if not normalized or fits_hook_length(normalized, max_words=max_words, max_chars=max_chars):
        return normalized

    sentences = split_sentences(normalized)
    while len(sentences) > 1 and not fits_hook_length(
        " ".join(sentences), max_words=max_words, max_chars=max_chars
    ):
        sentences.pop()

    return " ".join(sentences).strip()


def _is_truncated_fact(fact: str) -> bool:
    s = (fact or "").strip()
    return s.endswith("…") or s.endswith("...")


def _clean_teaser(fact: str) -> str:
    """Clean a complete fact for natural spoken TTS delivery without ever chopping mid-sentence."""
    text = re.sub(r"\s+", " ", (fact or "")).strip()
    if not text or _is_truncated_fact(text):
        return ""
    text = text.replace("—", ", ").replace("–", ", ")
    text = re.sub(r"#(\d+)", r"number \1", text)
    text = text.replace("&", "and")

    # Keep the first complete sentence if multiple sentences were concatenated
    parts = split_sentences(text)
    if not parts:
        return ""
    text = parts[0].strip()

    if text and not text.endswith((".", "!", "?")):
        text += "!"
    return text


def _candidate_hook_facts(facts: Optional[List[str]]) -> List[str]:
    """Return all cleaned, complete (non-truncated) facts in priority order."""
    if not facts:
        return []
    candidates: List[str] = []
    for f in facts:
        if not isinstance(f, str):
            continue
        cleaned = _clean_teaser(f)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


def _pick_best_hook_fact(facts: Optional[List[str]]) -> str:
    """Choose the punchiest complete fact (never using ellipsis-truncated snippets)."""
    candidates = _candidate_hook_facts(facts)
    if not candidates:
        return ""
    best = candidates[0]
    if len(best.split()) > HOOK_MAX_WORDS:
        shorter = [f for f in candidates if len(f.split()) <= HOOK_MAX_WORDS - 2]
        if shorter:
            best = shorter[0]
    return best


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
    max_words: int = HOOK_MAX_WORDS,
    max_chars: int = HOOK_MAX_CHARS,
) -> Dict[str, str]:
    """
    Build a bright, upbeat 5-8s era-specific GenX DJ spoken hook.
    Checks that the rendered script fits within the hook length definitions
    (max_words / max_chars); if not, tries a shorter complete fact or reduces
    the number of sentences so the hook fits while remaining grammatical.
    Returns dict: {"hook_id": str, "era": str, "text": str}.
    """
    year = track.get("year")
    era = get_era_bucket(year)

    if custom_text and custom_text.strip():
        return {
            "hook_id": "custom",
            "era": era,
            "text": fit_hook_text_to_limits(
                custom_text, max_words=max_words, max_chars=max_chars
            ),
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
    candidate_facts = _candidate_hook_facts(facts)

    def _render(tmpl: Dict[str, str], fact_text: str) -> str:
        raw = tmpl["template"].format(
            artist=artist,
            title=title,
            year=year_str,
            peak_phrase=peak_phrase,
            fact=fact_text,
        )
        return re.sub(r"\s+", " ", raw).strip()

    # Try each complete fact in priority order to see if template + fact fits limits
    rendered: Optional[str] = None
    for fact_teaser in candidate_facts:
        candidate = _render(chosen, fact_teaser)
        if fits_hook_length(candidate, max_words=max_words, max_chars=max_chars):
            rendered = candidate
            break

    if rendered is None:
        # No fact fit with this template (or no complete facts available).
        # Avoid templates whose opener doesn't stand cleanly without a fact.
        if chosen["id"] in _FACT_REQUIRED_TEMPLATE_IDS:
            standalone_pool = [
                t for t in fresh if t["id"] not in _FACT_REQUIRED_TEMPLATE_IDS
            ] or [
                t for t in combined if t["id"] not in _FACT_REQUIRED_TEMPLATE_IDS
            ]
            chosen = r.choice(standalone_pool)
            for fact_teaser in candidate_facts:
                candidate = _render(chosen, fact_teaser)
                if fits_hook_length(candidate, max_words=max_words, max_chars=max_chars):
                    rendered = candidate
                    break

    if rendered is None:
        # Render without fact (or with first fact if single-sentence reduction needed)
        # and reduce whole sentences until it fits within the hook length definitions.
        base = _render(chosen, "")
        rendered = fit_hook_text_to_limits(
            base, max_words=max_words, max_chars=max_chars
        )

    return {
        "hook_id": chosen["id"],
        "era": chosen["era"],
        "text": rendered,
    }
