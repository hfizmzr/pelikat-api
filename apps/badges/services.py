"""
Badge Evaluation Engine

Queries runner aggregate stats from Supabase, evaluates badge rules,
and inserts newly earned badges via service_role key.
"""

from supabase import create_client
from django.conf import settings

def get_sb():
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY
    )

BADGE_RULES = [
    {
        "badge_key": "first_run",
        "name": "First Run",
        "description": "Completed your first virtual run",
        "icon": "🏃",
        "meter": lambda s: s["total_runs"] >= 1,
    },
    {
        "badge_key": "5k_distance",
        "name": "5K Club",
        "description": "Ran a total of 5 kilometers",
        "icon": "🎯",
        "meter": lambda s: s["total_distance"] >= 5.0,
    },
    {
        "badge_key": "10k_distance",
        "name": "10K Club",
        "description": "Ran a total of 10 kilometers",
        "icon": "🏆",
        "meter": lambda s: s["total_distance"] >= 10.0,
    },
    {
        "badge_key": "21k_distance",
        "name": "Half Marathon",
        "description": "Ran a total of 21.1 kilometers",
        "icon": "🥈",
        "meter": lambda s: s["total_distance"] >= 21.1,
    },
    {
        "badge_key": "42k_distance",
        "name": "Full Marathon",
        "description": "Ran a total of 42.2 kilometers",
        "icon": "🌟",
        "meter": lambda s: s["total_distance"] >= 42.2,
    },
    {
        "badge_key": "100k_distance",
        "name": "100K Club",
        "description": "Ran a total of 100 kilometers",
        "icon": "💎",
        "meter": lambda s: s["total_distance"] >= 100.0,
    },
    {
        "badge_key": "5_runs",
        "name": "5 Runs",
        "description": "Logged 5 virtual runs",
        "icon": "🏃‍♂️",
        "meter": lambda s: s["total_runs"] >= 5,
    },
    {
        "badge_key": "10_runs",
        "name": "10 Runs",
        "description": "Logged 10 virtual runs",
        "icon": "🏃‍♀️",
        "meter": lambda s: s["total_runs"] >= 10,
    },
    {
        "badge_key": "25_runs",
        "name": "25 Runs",
        "description": "Logged 25 virtual runs",
        "icon": "💪",
        "meter": lambda s: s["total_runs"] >= 25,
    },
    {
        "badge_key": "50_runs",
        "name": "50 Runs",
        "description": "Logged 50 virtual runs",
        "icon": "🔥",
        "meter": lambda s: s["total_runs"] >= 50,
    },
    {
        "badge_key": "streak_3",
        "name": "3-Day Streak",
        "description": "Ran on 3 consecutive days",
        "icon": "📅",
        "meter": lambda s: s["current_streak"] >= 3,
    },
    {
        "badge_key": "streak_7",
        "name": "7-Day Streak",
        "description": "Ran on 7 consecutive days",
        "icon": "🔥",
        "meter": lambda s: s["current_streak"] >= 7,
    },
    {
        "badge_key": "streak_30",
        "name": "30-Day Streak",
        "description": "Ran on 30 consecutive days",
        "icon": "⚡",
        "meter": lambda s: s["current_streak"] >= 30,
    },
]


def evaluate_badges(runner_id: str, event_id: str | None = None) -> dict:
    """
    Evaluate all badge rules for a runner and award any newly earned badges.

    Returns dict with 'awarded' list of newly-earned badge definitions.
    """
    sb = get_sb()

    stats = _gather_stats(sb, runner_id)
    existing = _get_existing_badge_keys(sb, runner_id, event_id)

    awarded = []
    for rule in BADGE_RULES:
        if rule["badge_key"] in existing:
            continue
        try:
            if rule["meter"](stats):
                sb.table("runner_badges").insert({
                    "runner_id": runner_id,
                    "event_id": event_id,
                    "badge_key": rule["badge_key"],
                }).execute()
                awarded.append({
                    "badge_key": rule["badge_key"],
                    "name": rule["name"],
                    "description": rule["description"],
                    "icon": rule["icon"],
                })
        except Exception:
            pass

    return {"awarded": awarded}


def get_badge_definitions() -> list[dict]:
    return [
        {"badge_key": r["badge_key"], "name": r["name"],
         "description": r["description"], "icon": r["icon"]}
        for r in BADGE_RULES
    ]


def _gather_stats(sb, runner_id: str) -> dict:
    total_runs = 0
    total_distance = 0.0
    current_streak = 0

    logs = sb.table("run_logs").select("distance_km").eq("runner_id", runner_id).execute()
    if logs.data:
        total_runs = len(logs.data)
        total_distance = sum(float(r.get("distance_km", 0)) for r in logs.data)

    streak = sb.table("runner_streaks").select("current_streak").eq("runner_id", runner_id).maybe_single().execute()
    if streak and streak.data:
        current_streak = int(streak.data.get("current_streak", 0))

    return {
        "total_runs": total_runs,
        "total_distance": total_distance,
        "current_streak": current_streak,
    }


def _get_existing_badge_keys(sb, runner_id: str, event_id: str | None) -> set:
    query = sb.table("runner_badges").select("badge_key").eq("runner_id", runner_id)
    if event_id:
        query = query.eq("event_id", event_id)
    else:
        query = query.is_("event_id", "null")
    result = query.execute()
    return {r["badge_key"] for r in (result.data or [])}
