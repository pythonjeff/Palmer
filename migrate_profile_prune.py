"""One-off: prune profiles down to PROFILE_FIELDS, preserving what matters.

Why this exists
---------------
The per-turn Haiku extractor writes whatever keys it likes, and nothing bounded
that until userprofile.PROFILE_FIELDS was added. One profile had grown to 624
keys, 604 of them one-offs ("monday_night_behavior", "kendrick_fan",
"tv_taste_update", "alternatively"). The whole profile is dumped as JSON into
every system prompt, so that was ~21,700 tokens of noise per turn — roughly
double SYSTEM_PROMPT and the tool schemas combined — burying the 20 keys that
actually mattered and costing real money on every message.

The allow-list stops the regrowth. This cleans what already accumulated.

It does NOT just delete. Those stray keys hold real facts about a person, so a
Sonnet pass folds them into the canonical fields (life_summary, interests,
relationships, communication_style, ...) first; only then is the rest dropped.

Usage
-----
    python migrate_profile_prune.py                 # dry run, every profile
    python migrate_profile_prune.py --apply         # write changes
    python migrate_profile_prune.py --phone +1555…  # single profile

On Heroku (DATABASE_URL is set there, so it targets Postgres):

    heroku run python migrate_profile_prune.py -a palmer-app
    heroku run python migrate_profile_prune.py --apply -a palmer-app

Dry run is the default and prints a full audit — key counts, prompt-token
before/after, and every field name being dropped — so the change is reviewable
before anything is written.
"""
from dotenv import load_dotenv
load_dotenv()

import argparse
import json

from db import get_all_profiles, upsert_profile, get_profile
from llm import client, SONNET_MODEL, _parse_json
from userprofile import PROFILE_FIELDS, prune_profile

# Fields the consolidation pass is allowed to rewrite. Deliberately excludes
# scheduling and bookkeeping keys — folding stray notes must never move someone's
# briefing time or clear a send guard.
_MERGE_TARGETS = (
    "interests", "life_summary", "life_context", "communication_style",
    "relationships", "job", "vibe", "sports_teams", "brands", "ongoing_threads",
)

_PROMPT = """These are stray fields that accumulated on one person's profile — a language model invented a new key most turns instead of updating the existing ones. They hold real information about this person mixed with noise and duplication.

Fold what genuinely matters into the canonical fields below. Everything else is discarded, so anything you leave out is lost for good.

Canonical fields you may write:
{targets}

Current values:
{current}

Stray fields:
{stray}

Merge and deduplicate. Keep durable facts, preferences and relationships. Drop anything transient, anything already captured, and anything that was only ever an artifact of how the extractor phrased itself. Return ONLY a JSON object of the canonical fields you are updating."""


def _consolidate(kept: dict, stray: dict) -> dict:
    """Fold stray fields into canonical ones. Returns {} on any failure — the
    caller then prunes without merging, which loses detail but never corrupts."""
    if not stray:
        return {}
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": _PROMPT.format(
                targets="\n".join(f"  {t}" for t in _MERGE_TARGETS),
                current=json.dumps({k: v for k, v in kept.items() if k in _MERGE_TARGETS},
                                   indent=1)[:3000],
                stray=json.dumps(stray, indent=1)[:60000],
            )}],
        )
        parsed = _parse_json(response.content[0].text)
    except Exception as e:
        print(f"    consolidation failed ({type(e).__name__}: {e}) — pruning without merge")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if k in _MERGE_TARGETS and k in PROFILE_FIELDS}


def _clean_topics(profile: dict) -> dict:
    """Pull non-topics out of morning_topics.

    morning_topics is itself a schema field, so pruning doesn't touch what's
    inside it. Users answer "what do you want each morning?" with delivery
    preferences and routes as well as subjects — those are filtered at read time
    now, but they still cost prompt tokens and still read as instructions.
    A route is promoted to the structured `commute` field rather than dropped.
    """
    from morning import _is_directive, _parse_commute_topic
    topics = profile.get("morning_topics") or []
    if not topics:
        return {}
    kept, updates = [], {}
    for t in topics:
        route = _parse_commute_topic(t)
        if route and not profile.get("commute"):
            updates["commute"] = {"origin": route[0], "destination": route[1]}
            continue
        if route or _is_directive(t):
            continue
        kept.append(t)
    if len(kept) != len(topics):
        updates["morning_topics"] = kept
    return updates


def _size(profile: dict) -> int:
    """Characters this profile adds to every system prompt."""
    return len(json.dumps(profile, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    ap.add_argument("--phone", help="limit to one phone number")
    args = ap.parse_args()

    profiles = get_all_profiles()
    if args.phone:
        profiles = [(p, prof) for p, prof in profiles if p == args.phone]
    if not profiles:
        print("no matching profiles")
        return

    print(f"{'APPLY' if args.apply else 'DRY RUN'} — {len(profiles)} profile(s)\n")
    total_before = total_after = 0

    for phone, profile in profiles:
        kept, dropped = prune_profile(profile)
        before = _size(profile)
        if not dropped:
            topic_fix = _clean_topics(profile)
            if topic_fix:
                print(f"{phone}: schema clean, but morning_topics needs tidying")
                print(f"    {', '.join(sorted(topic_fix))}")
                if args.apply:
                    upsert_profile(phone, topic_fix)
                    print("    written.")
            else:
                print(f"{phone}: already clean ({len(kept)} fields, ~{before // 4} tokens)")
            total_before += before
            total_after += before
            continue

        stray = {k: profile[k] for k in dropped}
        merged = _consolidate(kept, stray)
        merged.update(_clean_topics(profile))
        final = dict(kept)
        final.update(merged)
        after = _size(final)
        total_before += before
        total_after += after

        print(f"{phone}:")
        print(f"    fields   {len(profile)} -> {len(final)}   ({len(dropped)} dropped)")
        print(f"    prompt   ~{before // 4} -> ~{after // 4} tokens per message")
        print(f"    merged into: {', '.join(sorted(merged)) or '(nothing — pruned only)'}")
        print(f"    dropping: {', '.join(sorted(dropped)[:20])}"
              + (f" … +{len(dropped) - 20} more" if len(dropped) > 20 else ""))

        if args.apply:
            # upsert_profile merges, so dropped keys must be sent explicitly.
            # None deletes (see db.upsert_profile).
            updates = dict(merged)
            for k in dropped:
                updates[k] = None
            upsert_profile(phone, updates)
            _, leftover = prune_profile(get_profile(phone))
            print(f"    written — {len(leftover)} non-schema field(s) remaining"
                  + (" ✓" if not leftover else f" ✗ {leftover[:5]}"))
        print()

    print(f"total prompt cost: ~{total_before // 4} -> ~{total_after // 4} tokens per message")
    if not args.apply:
        print("\ndry run — nothing written. re-run with --apply to commit the change.")


if __name__ == "__main__":
    main()
