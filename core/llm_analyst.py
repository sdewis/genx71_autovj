"""
Local CPU-Efficient LLM Analyst for GenX71 AutoVJ Post Analytics.

Connects to the local Ollama sidecar (`llm-worker` on port 11434) running
Qwen3.5 2B (`qwen3.5:2b` — ~1.3 GB quantized Q4_K_M, Apache 2.0, Gated Delta
Networks hybrid architecture for ultra-fast CPU-only inference).

Produces structured daily analytics reports:
  - Executive summary
  - Top performing posts & success drivers
  - Actionable improvements
  - Structured track-selection feedback weights (genre, decade, region, wildcard %)

If the local LLM sidecar is offline or unreachable, gracefully falls back to a
deterministic statistical analyzer so scheduled 2 AM reports never fail.
"""

import os
import re
import json
import time
import logging
from typing import Any, Dict, List, Optional

import requests

from core.db import get_setting

logger = logging.getLogger("genx71.llm_analyst")

DEFAULT_LLM_MODEL = "qwen3.5:2b"
DEFAULT_LLM_URL = os.environ.get("LLM_WORKER_URL", "http://127.0.0.1:11434")

RECOMMENDED_LLM_MODELS: List[Dict[str, str]] = [
    {
        "id": "qwen3.5:2b",
        "label": "Qwen 3.5 2B (Default — Best CPU Efficiency, ~1.3 GB RAM)",
        "size": "1.3 GB",
    },
    {
        "id": "qwen3.5:4b",
        "label": "Qwen 3.5 4B (Deeper Reasoning, ~2.7 GB RAM)",
        "size": "2.7 GB",
    },
    {
        "id": "qwen3:1.7b",
        "label": "Qwen 3 1.7B (Ultra-Light CPU, ~1.1 GB RAM)",
        "size": "1.1 GB",
    },
    {
        "id": "phi4-mini",
        "label": "Phi-4 Mini 3.8B (Microsoft CPU-Friendly, ~2.5 GB RAM)",
        "size": "2.5 GB",
    },
]


def resolve_llm_url(db_path: Optional[str] = None) -> str:
    """Resolve the Ollama base URL from settings, environment, or local default."""
    setting_url = (get_setting("llm_worker_url", "", db_path=db_path) or "").strip()
    if setting_url:
        return setting_url.rstrip("/")
    env_url = (os.environ.get("LLM_WORKER_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    return "http://127.0.0.1:11434"


def resolve_llm_model(db_path: Optional[str] = None) -> str:
    """Resolve the configured local LLM model tag (defaults to qwen3.5:2b)."""
    model = (get_setting("llm_model", DEFAULT_LLM_MODEL, db_path=db_path) or "").strip()
    return model or DEFAULT_LLM_MODEL


def _model_matches(configured: str, installed_name: str) -> bool:
    c = configured.strip().lower()
    i = installed_name.strip().lower()
    if c == i:
        return True
    if ":" not in c and i == f"{c}:latest":
        return True
    return False


def get_llm_health(db_path: Optional[str] = None, timeout: float = 3.0) -> Dict[str, Any]:
    """Check local Ollama sidecar liveness and whether the configured model is pulled."""
    base_url = resolve_llm_url(db_path=db_path)
    model = resolve_llm_model(db_path=db_path)
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        models = [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]
        installed = any(_model_matches(model, m) for m in models)
        return {
            "online": True,
            "url": base_url,
            "configured_model": model,
            "model_installed": installed,
            "installed_models": models,
            "recommended_models": RECOMMENDED_LLM_MODELS,
            "error": None,
        }
    except Exception as exc:
        return {
            "online": False,
            "url": base_url,
            "configured_model": model,
            "model_installed": False,
            "installed_models": [],
            "recommended_models": RECOMMENDED_LLM_MODELS,
            "error": str(exc),
        }


def ensure_model_pulled(
    model: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    """Ensure the configured local LLM model is downloaded in Ollama."""
    base_url = resolve_llm_url(db_path=db_path)
    target_model = (model or resolve_llm_model(db_path=db_path)).strip()
    health = get_llm_health(db_path=db_path)
    if not health["online"]:
        raise RuntimeError(f"Local LLM worker at {base_url} is offline: {health['error']}")

    if any(_model_matches(target_model, m) for m in health["installed_models"]):
        return {"ok": True, "model": target_model, "already_installed": True}

    logger.info("Pulling local CPU-efficient LLM '%s' from Ollama (%s)...", target_model, base_url)
    t0 = time.monotonic()
    resp = requests.post(
        f"{base_url}/api/pull",
        json={"name": target_model, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    elapsed = round(time.monotonic() - t0, 1)
    logger.info("Pulled '%s' in %.1fs", target_model, elapsed)
    return {"ok": True, "model": target_model, "already_installed": False, "elapsed_sec": elapsed}


def _clamp_weight(val: Any, lo: float = 0.5, hi: float = 2.0, default: float = 1.0) -> float:
    try:
        f = float(val)
        if f != f:  # NaN
            return default
        return round(max(lo, min(hi, f)), 2)
    except (TypeError, ValueError):
        return default


def sanitize_feedback_weights(raw: Any) -> Dict[str, Any]:
    """Validate and clamp LLM-produced feedback weights so track selection stays safe & diverse."""
    if not isinstance(raw, dict):
        raw = {}

    def _clean_map(d: Any) -> Dict[str, float]:
        if not isinstance(d, dict):
            return {}
        out: Dict[str, float] = {}
        for k, v in d.items():
            key = str(k).strip()
            if not key or len(key) > 40:
                continue
            out[key] = _clamp_weight(v)
        return out

    rec_wc = raw.get("recommended_wildcard_chance")
    try:
        wc_int = max(0, min(100, int(round(float(rec_wc))))) if rec_wc is not None else None
    except (TypeError, ValueError):
        wc_int = None

    pref_tpls = raw.get("preferred_templates")
    if isinstance(pref_tpls, list):
        clean_tpls = [str(x).strip().lower() for x in pref_tpls if str(x).strip()][:4]
    else:
        clean_tpls = []

    avoid_genres = raw.get("avoid_genres")
    if isinstance(avoid_genres, list):
        clean_avoid = [str(x).strip() for x in avoid_genres if str(x).strip()][:5]
    else:
        clean_avoid = []

    rationale = str(raw.get("rationale") or "").strip()[:400]

    return {
        "preferred_genres": _clean_map(raw.get("preferred_genres")),
        "preferred_decades": _clean_map(raw.get("preferred_decades")),
        "preferred_regions": _clean_map(raw.get("preferred_regions")),
        "recommended_wildcard_chance": wc_int,
        "preferred_templates": clean_tpls,
        "avoid_genres": clean_avoid,
        "rationale": rationale,
    }


def _extract_json_object(text: str) -> Dict[str, Any]:
    """Robustly parse a JSON object from LLM text, stripping markdown fences or <think> tags."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed = json.loads(cleaned[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("LLM did not return a valid JSON object.")


def build_statistical_analysis(analytics_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic fallback analytics engine when local Ollama is offline or has no model yet.
    Computes genuine data-driven insights and feedback weights from post performance metrics.
    """
    totals = analytics_payload.get("totals", {})
    top_rows = analytics_payload.get("top_posts", [])[:5]
    breakdowns = analytics_payload.get("breakdowns", {})

    tracked = int(totals.get("tracked_posts", 0))
    total_views = int(totals.get("total_views", 0))
    avg_eng = float(totals.get("avg_engagement_rate", 0.0))
    avg_comp = float(totals.get("avg_completion_rate", 0.0))

    if tracked == 0:
        return {
            "summary_markdown": (
                "No post engagement metrics have been recorded yet. "
                "Sync analytics from TikTok Studio or enter post views/likes/completion rates "
                "to unlock personalized daily AI reports and track-choice feedback."
            ),
            "top_posts": [],
            "improvements": [
                "Sync your existing TikTok posts or enter views & likes for your recent uploads.",
                "Test 2–3 different visual style packs (e.g., VHS, Neon, Hi-Fi) to establish a baseline.",
                "Try enabling regional UK/US DJ Voice Hook TTS on a few posts to compare completion rates.",
            ],
            "feedback_weights": sanitize_feedback_weights({
                "rationale": "Baseline neutral weights — waiting for initial post performance metrics.",
            }),
        }

    def _weights_from_breakdown(items: List[Dict[str, Any]]) -> Dict[str, float]:
        if not items:
            return {}
        overall_avg = sum(i.get("avg_score", 0.0) for i in items) / max(len(items), 1)
        if overall_avg <= 0:
            return {}
        weights: Dict[str, float] = {}
        for item in items:
            ratio = item.get("avg_score", 0.0) / overall_avg
            # Dampen ratio into [0.7, 1.6]
            weights[item["label"]] = _clamp_weight(0.5 + 0.5 * ratio, lo=0.7, hi=1.6)
        return weights

    genre_weights = _weights_from_breakdown(breakdowns.get("by_genre", []))
    decade_weights = _weights_from_breakdown(breakdowns.get("by_decade", []))
    region_weights = _weights_from_breakdown(breakdowns.get("by_region", []))

    top_templates = [
        item["label"]
        for item in breakdowns.get("by_template", [])[:2]
        if item.get("avg_score", 0) > 0
    ]

    # Recommend wildcard chance based on wildcard vs Top-20 score
    wc_items = {item["label"]: item for item in breakdowns.get("by_wildcard", [])}
    rec_wc = None
    if "Wildcard" in wc_items and "Top 20 Hit" in wc_items:
        wc_score = wc_items["Wildcard"].get("avg_score", 0.0)
        hit_score = wc_items["Top 20 Hit"].get("avg_score", 0.0)
        if wc_score > hit_score * 1.15:
            rec_wc = 40
        elif wc_score < hit_score * 0.85:
            rec_wc = 15
        else:
            rec_wc = 25

    top_genre = breakdowns.get("by_genre", [{}])[0].get("label") if breakdowns.get("by_genre") else None
    top_decade = breakdowns.get("by_decade", [{}])[0].get("label") if breakdowns.get("by_decade") else None
    top_region = breakdowns.get("by_region", [{}])[0].get("label") if breakdowns.get("by_region") else None
    best_post = top_rows[0] if top_rows else None

    period_key = str(analytics_payload.get("period") or "daily")
    comp_info = analytics_payload.get("period_comparison") or {}
    learning = analytics_payload.get("learning_history") or {}

    summary_parts = [
        f"**{period_key.capitalize()} review:** Analyzed **{tracked} tracked post(s)** totaling **{total_views:,} views** "
        f"(avg engagement **{avg_eng:.2f}%**, avg completion **{avg_comp:.1f}%**)."
    ]
    if period_key in ("weekly", "monthly") and comp_info.get("previous_posts", 0) > 0:
        v_delta = comp_info.get("views_delta_pct")
        s_delta = comp_info.get("score_delta_pct")
        cmp_label = "week-over-week" if period_key == "weekly" else "month-over-month"
        delta_bits = []
        if v_delta is not None:
            delta_bits.append(f"avg views **{v_delta:+.1f}%**")
        if s_delta is not None:
            delta_bits.append(f"avg score **{s_delta:+.1f}%**")
        if delta_bits:
            summary_parts.append(f"Compared to the prior period ({cmp_label}): {', '.join(delta_bits)}.")
    if best_post:
        summary_parts.append(
            f"Top performer: **{best_post['artist']} — {best_post['title']} ({best_post['year']})** "
            f"with **{best_post['views']:,} views** and a score of **{best_post['performance_score']}**."
        )
    if top_genre and top_decade:
        summary_parts.append(
            f"Strongest audience resonance came from **{top_decade} {top_genre}** tracks"
            + (f" ({top_region} charts)" if top_region else "")
            + "."
        )

    improvements: List[str] = []
    if top_genre:
        improvements.append(
            f"Prioritize {top_genre} tracks in upcoming schedules — they lead your catalog in average performance score."
        )
    if top_decade:
        improvements.append(
            f"Lean into {top_decade} nostalgia hooks and trivia facts; {top_decade} posts outperformed other eras."
        )
    if top_templates:
        improvements.append(
            f"Use '{', '.join(top_templates)}' visual style pack(s) more frequently for hero tracks."
        )
    if rec_wc is not None:
        improvements.append(
            f"Adjust Wildcard (Top 21–40) chance toward {rec_wc}% based on Deep Cut vs. Top 20 engagement."
        )
    if avg_comp < 35.0 and tracked > 0:
        improvements.append(
            "Boost early retention by testing shorter, punchier DJ voice hooks or immediate chorus audio entry."
        )
    if not improvements:
        improvements.append("Continue posting consistently across styles and eras to build deeper trend confidence.")

    # Incorporate past suggestion successes/failures into lessons_learned and dampen failed boosts
    lessons_learned: List[str] = []
    past_successes = learning.get("recent_successes") or []
    past_failures = learning.get("recent_failures") or []
    for s in past_successes[:3]:
        lessons_learned.append(f"Confirmed win: {s}")
    for f in past_failures[:3]:
        lessons_learned.append(f"Course-correcting past miss: {f}")
        # Dampen any genre weight explicitly called out as underperforming in recent failures
        for g_name in list(genre_weights.keys()):
            if f"'{g_name.lower()}'" in f.lower() and "underperformed" in f.lower():
                genre_weights[g_name] = _clamp_weight(genre_weights[g_name] * 0.85, lo=0.65, hi=1.6)
        for d_name in list(decade_weights.keys()):
            if d_name.lower() in f.lower() and "underperformed" in f.lower():
                decade_weights[d_name] = _clamp_weight(decade_weights[d_name] * 0.85, lo=0.65, hi=1.6)

    if not lessons_learned and period_key in ("weekly", "monthly"):
        lessons_learned.append(
            "Baseline cycle established — future weekly/monthly runs will automatically grade these recommendations against subsequent posts."
        )

    formatted_top = [
        {
            "post_id": r["post_id"],
            "artist": r["artist"],
            "title": r["title"],
            "year": r["year"],
            "views": r["views"],
            "engagement_rate": r["engagement_rate"],
            "performance_score": r["performance_score"],
            "reason": f"{r.get('genre', 'GenX')} ({r.get('template_used', 'auto')} style) — {r['views']:,} views, {r['engagement_rate']}% engagement",
        }
        for r in top_rows
    ]

    rationale = (
        f"Boosted {top_genre or 'top genres'} and {top_decade or 'leading decades'} "
        f"based on {tracked} tracked post(s)"
        + (f" and {len(past_successes)} win(s) / {len(past_failures)} correction(s) from prior reports." if (past_successes or past_failures) else ".")
    )

    return {
        "summary_markdown": " ".join(summary_parts),
        "top_posts": formatted_top,
        "improvements": improvements[:6],
        "lessons_learned": lessons_learned[:6],
        "feedback_weights": sanitize_feedback_weights({
            "preferred_genres": genre_weights,
            "preferred_decades": decade_weights,
            "preferred_regions": region_weights,
            "recommended_wildcard_chance": rec_wc,
            "preferred_templates": top_templates,
            "avoid_genres": [],
            "rationale": rationale,
        }),
    }


def _build_llm_prompt(
    analytics_payload: Dict[str, Any],
    period: str = "daily",
    learning_history: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a compact, token-efficient prompt suited for 4K-context CPU local LLMs."""
    totals = analytics_payload.get("totals", {})
    breakdowns = analytics_payload.get("breakdowns", {})
    top_posts = analytics_payload.get("top_posts", [])[:8]
    bottom_posts = analytics_payload.get("bottom_posts", [])[:4]
    period_comparison = analytics_payload.get("period_comparison") or {}
    learning = learning_history or analytics_payload.get("learning_history") or {}

    compact_learning = {
        "evaluated_reports": learning.get("evaluated_reports", 0),
        "improved_count": learning.get("improved_count", 0),
        "regressed_count": learning.get("regressed_count", 0),
        "past_suggestion_successes": (learning.get("recent_successes") or [])[:4],
        "past_suggestion_failures": (learning.get("recent_failures") or [])[:4],
    }

    compact_data = {
        "period": period,
        "period_comparison": period_comparison,
        "totals": totals,
        "learning_history": compact_learning,
        "by_genre": breakdowns.get("by_genre", [])[:8],
        "by_decade": breakdowns.get("by_decade", []),
        "by_region": breakdowns.get("by_region", []),
        "by_wildcard": breakdowns.get("by_wildcard", []),
        "by_template": breakdowns.get("by_template", [])[:8],
        "by_tts": breakdowns.get("by_tts", []),
        "top_posts": [
            {
                "post_id": p["post_id"],
                "artist": p["artist"],
                "title": p["title"],
                "year": p["year"],
                "genre": p.get("genre"),
                "region": p.get("chart_region"),
                "wildcard": bool(p.get("is_wildcard")),
                "template": p.get("template_used"),
                "tts": bool(p.get("tts_enabled")),
                "views": p["views"],
                "likes": p["likes"],
                "comments": p["comments"],
                "shares": p["shares"],
                "completion_rate": p["completion_rate"],
                "engagement_rate": p["engagement_rate"],
                "score": p["performance_score"],
            }
            for p in top_posts
        ],
        "lower_posts": [
            {
                "artist": p["artist"],
                "title": p["title"],
                "year": p["year"],
                "genre": p.get("genre"),
                "template": p.get("template_used"),
                "views": p["views"],
                "engagement_rate": p["engagement_rate"],
                "score": p["performance_score"],
            }
            for p in bottom_posts
        ],
    }

    period_focus = {
        "daily": "Daily tactical review: spot immediate winners and quick wins for tomorrow's posts.",
        "weekly": "Weekly strategic review (7-day window vs prior 7 days): evaluate week-over-week trends and grade last week's AI suggestions.",
        "monthly": "Monthly strategic review (30-day window vs prior 30 days): identify durable audience shifts, fatigue, and long-term lessons from past AI successes & failures.",
    }.get(period, "Daily tactical review.")

    return (
        "You are a concise, self-improving TikTok Music Channel Data Analyst for GenX71 AutoVJ (1978-1996 chart hits).\n"
        f"Mode: {period_focus}\n"
        "Review `learning_history` carefully: double down on past_suggestion_successes, do NOT repeat past_suggestion_failures, "
        "and explain what you adjusted in `lessons_learned`.\n"
        "Analyze the JSON metrics below and return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "summary_markdown": "2-4 sentence executive summary highlighting trends, what worked best, and period-over-period change",\n'
        '  "top_posts": [{"post_id": 1, "artist": "...", "title": "...", "reason": "short 1-sentence reason why it succeeded"}],\n'
        '  "improvements": ["3 to 5 specific, actionable suggestions to improve views, retention, and future track selection"],\n'
        '  "lessons_learned": ["1 to 4 reflections on what past AI suggestions succeeded or failed and how weights were adapted"],\n'
        '  "feedback_weights": {\n'
        '    "preferred_genres": {"GenreName": 1.35},\n'
        '    "preferred_decades": {"1980s": 1.25, "1990s": 1.0},\n'
        '    "preferred_regions": {"UK": 1.2, "US": 1.0},\n'
        '    "recommended_wildcard_chance": 25,\n'
        '    "preferred_templates": ["vhs", "neon"],\n'
        '    "avoid_genres": [],\n'
        '    "rationale": "1 sentence explaining how these weights improve future track choices"\n'
        "  }\n"
        "}\n"
        "Rules:\n"
        "- Weight values must be numbers between 0.5 and 2.0 (1.0 = neutral, >1.0 = boost, <1.0 = reduce).\n"
        "- recommended_wildcard_chance must be an integer 0-100.\n"
        "- Base all conclusions strictly on the provided metrics and learning_history.\n\n"
        f"DATA:\n{json.dumps(compact_data, separators=(',', ':'))}"
    )


def analyze_posts_with_llm(
    analytics_payload: Dict[str, Any],
    db_path: Optional[str] = None,
    auto_pull: bool = True,
    timeout: int = 240,
    period: str = "daily",
    learning_history: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run the local CPU-efficient LLM (Ollama) on post analytics data.
    Incorporates period-over-period trends and closed-loop learning_history.
    Automatically falls back to deterministic statistical analysis if Ollama is offline.
    """
    t0 = time.monotonic()
    enriched_payload = {
        **analytics_payload,
        "period": period or analytics_payload.get("period") or "daily",
        "learning_history": learning_history or analytics_payload.get("learning_history") or {},
    }
    totals = enriched_payload.get("totals", {})
    tracked_posts = int(totals.get("tracked_posts", 0))

    if tracked_posts == 0:
        fallback = build_statistical_analysis(enriched_payload)
        return {
            **fallback,
            "model_used": "statistical-baseline",
            "status": "completed",
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "error_message": None,
        }

    base_url = resolve_llm_url(db_path=db_path)
    model = resolve_llm_model(db_path=db_path)
    prompt = _build_llm_prompt(enriched_payload, period=enriched_payload["period"], learning_history=enriched_payload["learning_history"])

    try:
        health = get_llm_health(db_path=db_path)
        if not health["online"]:
            raise RuntimeError(f"Ollama offline at {base_url}: {health['error']}")

        if not health["model_installed"]:
            if auto_pull:
                ensure_model_pulled(model=model, db_path=db_path)
            else:
                raise RuntimeError(f"Model '{model}' is not installed in Ollama yet.")

        logger.info("Running local LLM %s analytics with '%s' on %d tracked posts...", enriched_payload["period"], model, tracked_posts)
        resp = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise analytics engine. Output strictly valid JSON with no extra text.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": "json",
                "think": False,
                "keep_alive": "5m",
                "options": {
                    "num_ctx": 4096,
                    "temperature": 0.3,
                    "num_predict": 1000,
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_content = ((data.get("message") or {}).get("content") or "").strip()
        parsed = _extract_json_object(raw_content)

        # Merge with statistical baseline to guarantee complete fields
        baseline = build_statistical_analysis(enriched_payload)
        summary = str(parsed.get("summary_markdown") or "").strip() or baseline["summary_markdown"]

        raw_top = parsed.get("top_posts")
        top_by_id = {p["post_id"]: p for p in enriched_payload.get("top_posts", [])}
        cleaned_top: List[Dict[str, Any]] = []
        if isinstance(raw_top, list):
            for item in raw_top[:5]:
                if not isinstance(item, dict):
                    continue
                pid = item.get("post_id")
                src = top_by_id.get(pid, {})
                cleaned_top.append({
                    "post_id": pid,
                    "artist": item.get("artist") or src.get("artist") or "",
                    "title": item.get("title") or src.get("title") or "",
                    "year": src.get("year"),
                    "views": src.get("views", 0),
                    "engagement_rate": src.get("engagement_rate", 0.0),
                    "performance_score": src.get("performance_score", 0.0),
                    "reason": str(item.get("reason") or "").strip(),
                })
        if not cleaned_top:
            cleaned_top = baseline["top_posts"]

        raw_impr = parsed.get("improvements")
        if isinstance(raw_impr, list):
            cleaned_impr = [str(x).strip() for x in raw_impr if str(x).strip()][:6]
        else:
            cleaned_impr = []
        if not cleaned_impr:
            cleaned_impr = baseline["improvements"]

        raw_lessons = parsed.get("lessons_learned")
        if isinstance(raw_lessons, list):
            cleaned_lessons = [str(x).strip() for x in raw_lessons if str(x).strip()][:6]
        else:
            cleaned_lessons = []
        if not cleaned_lessons:
            cleaned_lessons = baseline.get("lessons_learned") or []

        weights = sanitize_feedback_weights(parsed.get("feedback_weights"))
        # Fill in any empty weight maps from statistical baseline so feedback is always rich
        base_w = baseline["feedback_weights"]
        for key in ("preferred_genres", "preferred_decades", "preferred_regions"):
            if not weights.get(key) and base_w.get(key):
                weights[key] = base_w[key]
        if weights.get("recommended_wildcard_chance") is None:
            weights["recommended_wildcard_chance"] = base_w.get("recommended_wildcard_chance")
        if not weights.get("rationale"):
            weights["rationale"] = base_w.get("rationale", "")

        elapsed = round(time.monotonic() - t0, 2)
        logger.info("Local LLM %s analytics report generated by '%s' in %.2fs", enriched_payload["period"], model, elapsed)
        return {
            "summary_markdown": summary,
            "top_posts": cleaned_top,
            "improvements": cleaned_impr,
            "lessons_learned": cleaned_lessons,
            "feedback_weights": weights,
            "model_used": model,
            "status": "completed",
            "elapsed_sec": elapsed,
            "error_message": None,
        }
    except Exception as exc:
        logger.warning("Local LLM analysis unavailable (%s); using statistical fallback.", exc)
        fallback = build_statistical_analysis(enriched_payload)
        return {
            **fallback,
            "model_used": "statistical-fallback",
            "status": "fallback",
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "error_message": str(exc),
        }
