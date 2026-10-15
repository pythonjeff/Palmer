# Sources

News source quality. Governs `sources.py`, `datafeeds._search_raw`, `trusted_sources.json`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Source quality is one gate, applied at the search call

Every news fact and every news link Palmer sends — watch alerts, the morning briefing, Palmer Home, and the conversation `web_search` — comes out of `datafeeds._search_raw` or `datafeeds._search`. Both apply `sources.py` before returning, so quality is decided in one place rather than by each caller.

It used to be per-caller, and the callers disagreed: `watches.py` ranked by tier, `home._fetch_headlines` sorted but then took `results[0]` regardless, and the conversation search did nothing at all and dropped the URL besides. Filtering at the search call is what makes a change here reach every surface at once.

`sources.py` deliberately imports nothing from Palmer. The helpers were in `watches.py`, which imports `datafeeds` — so putting the filter where the search happens required moving them below it.

Four gates, cheapest first:

1. **Blocklist** (`is_blocked`) — dropped outright, never ranked. Two structural kinds: press-release wires (`prnewswire`, `globenewswire`, `einpresswire`…), where the "article" is a paid placement wearing a news layout, and republishing aggregators (`msn.com`, `biztoc`, `newsbreak`, `news.google.com`…), whose copy is a worse link than the original that is almost always sitting next to it in the same result set. **Keep this structural.** Do not add an outlet because its reporting is weak — that judgment ages badly in a JSON file the way the `PRIVATE_COMPANIES` denylist did in `tickers.py`. Demote by leaving it off the allowlist instead.
2. **Relevance floor** (`meets_score`) — tier 3 must clear `min_score`; trusted sources get `TRUSTED_SCORE_RELIEF` (0.15) of slack. This is not politeness. The floor runs *before* the tier sort, and Tavily's score measures query-text match, which is precisely what an SEO content farm is built to win — so a flat floor cut the Reuters piece at 0.45 and kept the farm at 0.90, and the tier sort never got the chance to undo it. Ranking was already in place and still could not save the good source, because the good source was gone before ranking ran.
3. **Tier ordering** (`source_tier`, applied by `rank`) — 1 = premier newsroom, wire, or official (`.gov`/`.edu` at runtime), 2 = mainstream and reputable specialist, 3 = everything else. Sorts by `(tier, -score)` so a wire report beats a higher-scoring blog. `rank(trusted_only=True)` drops tier 3 entirely. Palmer Home asks for it **first**, then falls back — see below. Conversation and the morning briefing go straight to tier 3 as a last resort: an obscure-but-real source beats "nothing found".

**The page falls back too, and the allowlist is why it has to.** Trusted-only was not filtering junk off the page; it was dropping the best source that exists. `"Philadelphia Eagles news"` lost `philadelphiaeagles.com` at score 0.75 and `nbcsportsphiladelphia.com` at 0.61; `"St. Louis area news"` lost `fox2now`, `ksdk` and `stlamerican` — every real newsroom in the market — and returned an empty card instead. The allowlist is ~100 domains and there are thousands of local outlets and team sites; it will never cover them, and adding them one at a time is the maintenance trap the blocklist notes warn about.

So `home._fetch_headlines` tries trusted, and on nothing falls back to `trusted_only=False` at `UNTRUSTED_MIN_SCORE` (0.60) rather than the usual 0.5. **The higher bar is the point**: an unvetted source has to earn its place on match strength because it is not earning it on provenance. Measured across every real user topic: 65% returned something under trusted-only, 82% with the fallback, and the rows it recovers are `fox2now.com`, `philadelphiaeagles.com` and `fintechfutures.com` — not content farms. The one mill it did let through at 0.5 (`vocal.media`, 0.52) is cut by the 0.60 bar and blocklisted structurally as a user-generated platform.

**Where the losses actually are, when a topic returns nothing.** Every topic gets 10 raw results and essentially none are lost to the recency window — the filtering is all score and tier. Three distinct failure modes, and only the first is fixable in code: an authoritative local/specialist source that is not on the allowlist (fixed by the fallback); a vague query whose matches are all weak (`"US politics"` tops out at 0.36 — the floor is right, the topic is too broad); and an ambiguous topic (`"Kirkwood, MO news"` returns an IndyCar driver named Kirkwood). The last two are topic-quality problems, not gate problems.
4. **Corroboration** (`corroborated`) — a watch or daily alert will not fire unless ≥ 2 distinct canonical domains agree, OR ≥ 1 tier-1 source confirms. Single unknown-domain hits are how rumor and spam leak through.

Suffix matching is on a dot boundary in both directions, so `notreuters.com` and `reuters.com.evil.example` are both tier 3. Without that a domain launders itself into tier 1 and nothing looks wrong until it is.

`_search_raw` pulls **10** candidates, not 5. Tavily bills per search, not per result, and the recency window throws most of a page away — a 5-result pull that loses three to the 12-hour cutoff leaves the tier sort nothing to choose between, which is how a lone content farm ends up as the best available source.

`trusted_sources.json` is meant to be hand-edited with no code change, which makes its shape a runtime dependency: bare lowercase hosts, no scheme or path or `www.`, no duplicates, and **no domain in both `domains` and `blocked`** (blocking runs first, so such a domain is silently blocked while reading as trusted). `test_sources.py::TestSourceListIntegrity` enforces all of it, and requires every blocked entry to carry a `why` — that field is the guardrail keeping the list structural.

Watches then add their own gates on top of the shared ones: a strict criticality rubric, 12-hour recency, `_url_reachable` (HEAD, 405 counts as alive) so a dead top link falls through to the next result, per-watch cooldown (default 4h), a `DAILY_ALERT_MAX` cap, and a dedup check against recent alert summaries. When editing this pipeline, keep all gates — removing any one produced noisy or bad alerts historically.

**The morning briefing and `web_search` label each story with its domain** (`[reuters.com] headline`). The drafting model was previously handed a flat list with no provenance, so it could not tell a wire report from a content farm and had no way to attribute anything it repeated.
