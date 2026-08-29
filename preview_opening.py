"""Print the Opening section for a city without texting anyone.

Opening ships gated off, because the risk in it is taste rather than
correctness: a section that lists Applebee's, a restaurant-week promo and a
tribute band is worse than no section at all. This is the tool for judging that,
one metro at a time, before the default is flipped on.

    python preview_opening.py "Culver City"
    python preview_opening.py "Kirkwood, MO" "Woodland Hills, California"

Prints what each city would show, and what it cost to find out. With no
arguments it previews every city on file, which is the real check — the
question is never "does it work for LA", it is "does it embarrass us anywhere".
"""
import sys

from opening import opening_snapshot, _clear_caches, TMDB_API_KEY, TICKETMASTER_API_KEY


def _cities_on_file() -> list[str]:
    from db import get_all_profiles
    seen = []
    for _phone, profile in get_all_profiles():
        city = (profile or {}).get("city")
        if city and city not in seen:
            seen.append(city)
    return seen


def preview(city: str) -> None:
    print(f"\n{'=' * 62}\n{city}\n{'=' * 62}")
    _clear_caches()          # each city priced honestly, no carry-over
    rows = opening_snapshot({"city": city})
    if not rows:
        print("  (nothing — an empty section is a valid outcome)")
        return
    for r in rows:
        when = f"  [{r['when']}]" if r.get("when") else ""
        print(f"  {r.get('kind', '?'):6} {r['title']}{when}")
        if r.get("subtitle"):
            print(f"         {r['subtitle']}")
        print(f"         {r.get('source') or '-'}  {r.get('url') or ''}")


def main() -> None:
    if not TMDB_API_KEY:
        print("! TMDB_API_KEY unset — no movies or shows will appear.")
    if not TICKETMASTER_API_KEY:
        print("! TICKETMASTER_API_KEY unset — no concerts or festivals will appear.")

    cities = sys.argv[1:] or _cities_on_file()
    if not cities:
        print("No cities given and none on file.")
        return
    for city in cities:
        try:
            preview(city)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
    print(f"\n{len(cities)} city/cities previewed. Cost splits by source: the 2 "
          f"Tavily searches are cached per metro per WEEK, while Ticketmaster, "
          f"TMDB and the curation call refresh DAILY — those are free or nearly "
          f"so, and keying them weekly was what froze the section. Screens cost "
          f"no model call at all.")


if __name__ == "__main__":
    main()
