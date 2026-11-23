"""
Unit tests for DJ hook text generation, abbreviation-aware sentence splitting,
truncation prevention, and hook length enforcement (core/hooks.py).
"""

import random
import pytest

from core.hooks import (
    HOOK_MAX_CHARS,
    HOOK_MAX_WORDS,
    HOOK_TEMPLATES,
    _clean_teaser,
    build_dj_hook_script,
    fit_hook_text_to_limits,
    fits_hook_length,
    split_sentences,
)


def test_split_sentences_preserves_initials_and_abbreviations():
    cases = [
        "Inspired by T.S. Eliot's poem 'The Waste Land' and Grandmaster Flash's 'The Message'.",
        "Spent four weeks at UK #1 and was famously sampled on P.M. Dawn's 1991 #1 hit.",
        "Inspired by J.G. Ballard's 1973 novel 'Crash' and became a blueprint for industrial pop.",
        "Richard D. James originally wrote it to clear the dancefloor at the end of Cornwall raves.",
    ]
    for sentence in cases:
        parts = split_sentences(sentence)
        assert parts == [sentence]
        cleaned = _clean_teaser(sentence)
        assert len(cleaned) >= len(sentence) - 2


def test_split_sentences_splits_multi_sentence_hooks():
    text = "Turn this one all the way up! Here's Dr. Dre from 1992. Classic West Coast anthem."
    assert split_sentences(text) == [
        "Turn this one all the way up!",
        "Here's Dr. Dre from 1992.",
        "Classic West Coast anthem.",
    ]


def test_clean_teaser_rejects_ellipsis_truncated_facts():
    assert _clean_teaser("It was the first single released from their third studio…") == ""
    assert _clean_teaser("Recorded at Abbey Road Studios in London with producer...") == ""


def test_fit_hook_text_reduces_sentences_without_mid_sentence_cutoff():
    three_sentences = (
        "Pop this in your Walkman, it's 1985! "
        "Orchestral Manoeuvres in the Dark with Enola Gay. "
        "Named after the B-29 bomber that dropped the atomic bomb on Hiroshima in 1945."
    )
    assert not fits_hook_length(three_sentences)

    fitted = fit_hook_text_to_limits(three_sentences)
    assert fits_hook_length(fitted)
    assert fitted == (
        "Pop this in your Walkman, it's 1985! "
        "Orchestral Manoeuvres in the Dark with Enola Gay."
    )
    assert fitted.endswith((".", "!", "?"))


def test_fit_hook_text_reduces_to_single_sentence_when_two_sentences_still_exceed_limit():
    text = (
        "Pure eighties gold from 1985! "
        "This extraordinarily long second sentence keeps going and going well past the twenty six word limit for a radio intro."
    )
    fitted = fit_hook_text_to_limits(text, max_words=15, max_chars=100)
    assert fitted == "Pure eighties gold from 1985!"
    assert fits_hook_length(fitted, max_words=15, max_chars=100)


def test_build_dj_hook_script_prefers_shorter_complete_fact_before_dropping():
    track = {"artist": "A-ha", "title": "Take On Me", "year": 1985, "chart_peak": 1, "chart_region": "US"}
    facts = [
        "Truncated Wikipedia sentence that stops right in the middle of a phrase…",
        (
            "An overly verbose trivia sentence that describes the entire production history "
            "of the single across twenty five different words and multiple clauses."
        ),
        "Hit US #1 in October 1985.",
    ]
    for seed in range(25):
        hook = build_dj_hook_script(track, facts=facts, rng=random.Random(seed))
        assert fits_hook_length(hook["text"])
        assert "Truncated Wikipedia" not in hook["text"]
        assert not hook["text"].endswith("…")
        assert hook["text"].endswith((".", "!", "?"))


def test_build_dj_hook_script_never_produces_dangling_did_you_know_without_fact():
    track = {"artist": "A-ha", "title": "Take On Me", "year": 1985, "chart_peak": 1, "chart_region": "US"}
    # Force only univ_did_you_know to be "fresh" by marking every other template as recently used
    all_other_ids = [t["id"] for t in HOOK_TEMPLATES if t["id"] != "univ_did_you_know"]
    # Provide only truncated facts so no valid fact is available
    hook = build_dj_hook_script(
        track,
        facts=["Cut off mid-sentence…"],
        recent_hook_ids=all_other_ids,
        rng=random.Random(0),
    )
    assert hook["hook_id"] != "univ_did_you_know"
    assert fits_hook_length(hook["text"])
    assert "Cut off" not in hook["text"]


def test_build_dj_hook_script_enforces_limits_on_custom_text():
    track = {"artist": "A-ha", "title": "Take On Me", "year": 1985}
    long_custom = (
        "Welcome back to Gen X radio! "
        "Here is an unforgettable eighties synth-pop classic from 1985. "
        "This third sentence pushes the custom script well past the twenty six word and one hundred eighty character limit."
    )
    res = build_dj_hook_script(track, custom_text=long_custom)
    assert res["hook_id"] == "custom"
    assert fits_hook_length(res["text"])
    assert res["text"] == (
        "Welcome back to Gen X radio! "
        "Here is an unforgettable eighties synth-pop classic from 1985."
    )
