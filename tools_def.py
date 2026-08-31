"""Tool schemas passed to Sonnet on every turn.

One data source per tool, and the USE THE RIGHT TOOL block in prompts.py
must agree with what is defined here. See CLAUDE.md "Tool routing".
"""


TOOLS = [
    {
        "name": "web_search",
        "description": "Search for current news, sports scores, events, and general facts. Do NOT use for weather (use get_weather) or prices (use get_price). Include specific dates in queries when recency matters — e.g. 'Cardinals score July 26 2026' not 'Cardinals score'. Results include publish dates; if results look stale, say so rather than presenting old info as current.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get accurate weather — current conditions or multi-day forecast. Use for ANY weather question, never web_search.\n\nIf the user doesn't specify a location, pass their city from the profile block above — the exact value stored there, not a broader region you infer from conversation. If the user names a neighborhood, suburb, or specific city ('Culver City', 'Astoria', 'Evanston'), pass that exact place — never substitute the metro area it belongs to ('Los Angeles' for 'Culver City', 'New York' for 'Astoria', 'Chicago' for 'Evanston'). The geocoder takes the top fuzzy match for whatever string you send with no disambiguation of its own, so a vaguer name can silently return a different, nearby place's weather. If you genuinely don't know which specific place the user means, ask rather than guessing broader.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "The most specific place name known — a neighborhood/suburb/city exactly as the user or their profile states it, e.g. 'Culver City' not 'Los Angeles', 'Astoria' not 'New York'. Add state/country only to disambiguate a name that exists in multiple places."},
                "when": {"type": "string", "description": "When: 'now', 'today', 'tomorrow', 'this weekend', 'next saturday', or a date like '2026-08-02'. Defaults to today."},
            },
            "required": ["location"],
        },
    },
    {
        "name": "add_weather_location",
        "description": "Pin a SECOND (or third) place to the weather section of their Palmer Home page, shown alongside their primary city — a second home, family elsewhere, somewhere they check often. Use for 'also show me the weather in Holiday Shores', 'add Chicago weather too', 'can you put my mom's place on there too'. This does NOT change their primary city (still set by saying where they live, or a weather-topic add via update_morning_briefing) and it does NOT appear in the morning text — only on the page. A one-off question about weather somewhere else is still get_weather, not this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Place name as they said it, e.g. 'Holiday Shores, IL'. It is geocoded for you."},
            },
            "required": ["location"],
        },
    },
    {
        "name": "remove_weather_location",
        "description": "Drop a secondary weather location from their page. Pass text_match with part of the place name to drop one; omit it to drop all extras. Never removes their primary city — that isn't this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text_match": {"type": "string", "description": "Part of the place name, e.g. 'Holiday Shores'. Omit to remove every extra location."},
            },
            "required": [],
        },
    },
    {
        "name": "get_price",
        "description": "Get real-time price for crypto or stocks. Use for Bitcoin, Ethereum, other crypto, any stock ticker (AAPL, TSLA, SPY, QQQ), or a company name you don't have a ticker for — company names are resolved for you, so pass 'SpaceX' or 'Nvidia' rather than declining. Returns current price and % change. Call this before saying a company is private or untradeable; the live lookup is authoritative and your memory of listings is not.",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Crypto name or symbol (bitcoin, eth, doge) or stock ticker (AAPL, TSLA, SPY)"},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "set_reminder",
        "description": "Save a reminder to be sent at a future time. Call this whenever the user asks to be reminded about something.\n\nOne-time by default. If they want it to REPEAT, pass recurrence — never file a repeating ask as a single reminder and leave it at that, because it will fire once and then be silent forever with nothing to tell them it stopped. 'every day at 7', 'each morning', 'every Monday', 'on weekdays' all take recurrence.\n\nA repeating ask for INFORMATION — a daily score, a price, news on a topic — is not this tool. That is update_morning_briefing, which is the recurring content list. Use set_reminder with recurrence for a nudge to do something ('take your meds', 'move the car', 'call your mom'). Roughly: if Palmer has to go look something up to write the message, it belongs in the morning update.\n\ndue_at is the FIRST occurrence; a recurring reminder repeats from there at the same local wall-clock time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind the user about"},
                "due_at": {"type": "string", "description": "ISO 8601 UTC datetime for the FIRST (or only) send, e.g. 2026-07-21T20:00:00Z"},
                "recurrence": {
                    "type": "string",
                    "enum": ["daily", "weekdays", "weekly"],
                    "description": "Omit for a one-time reminder. 'daily' = every day, 'weekdays' = Mon-Fri only, 'weekly' = same day each week (taken from due_at). The local time of day is preserved across daylight-saving changes.",
                },
            },
            "required": ["text", "due_at"],
        },
    },
    {
        "name": "send_gif",
        "description": """Search for and send a GIF. Use sparingly — one well-timed GIF beats three mediocre ones.

THINK IN MEMES AND CULTURAL REFERENCES, not literal descriptions. Specific references return far better results than generic ones.

Good search strategies:
- Reaction memes: 'this is fine fire', 'Jim Halpert stare to camera', 'Michael Scott no god please no', 'concerned Kermit', 'Dwight head shake', 'Ron Swanson nod'
- Celebration: 'Leonardo DiCaprio cheers', 'Oprah you get a car', 'LeBron shimmy', 'Carlton dance'
- Disbelief/chaos: 'John Travolta confused', 'math lady meme', 'dog sitting in fire', 'it crowd fire'
- Awkward/cringe: 'Steve Carell no please', 'nervous laugh', 'that's what she said'
- Agreement/validation: 'Parks and Recreation treat yourself', 'Patrick Stewart yes', 'slow clap'
- Disappointment: 'Schitt's Creek ew', 'arrested development her', 'Tobias never nude'

Match the register: confusion → 'John Travolta confused', celebration → 'confetti explosion', someone being dramatic → 'Sarah Jessica Parker gasp'. Never search just 'funny' or 'happy' — too generic.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Specific meme or cultural reference search, e.g. 'Michael Scott no god please no' or 'this is fine fire'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "update_morning_briefing",
        "description": "Add or remove things the user tracks, or pause/resume their morning entirely. This ONE list drives both their morning update and their live page, so it is also how the page changes: a topic that names something tradeable becomes a live price row in Markets, and everything else becomes a followed subject.\n\nUse it whenever they ask to start or stop tracking anything, in whatever words they reach for — 'add Bitcoin to my morning', 'add Apple stock to markets', 'put Nvidia on my site', 'track Tesla for me', 'start following the Cardinals', 'drop the movie stuff', 'stop sending me sports'. Do not treat 'markets', 'my site', 'my page', and 'my morning' as different places; they are the same list and you update it the same way.\n\nFor a price, pass the company or asset plus the word stock or price ('Apple stock price', 'Bitcoin price') — the ticker is resolved and verified for you, so never refuse because you are unsure whether something is listed and never invent a ticker yourself. Say 'stop my morning' / 'pause mornings' for enabled=false, 'resume my morning' for enabled=true; topics are preserved when paused.\n\nThe Opening section is separate from topics and is tuned with opening_add / opening_remove, not by adding a topic string. If they say 'I want movie openings too', 'add concerts to my morning', 'stop telling me about restaurants' or 'no more shows', that is opening_add/opening_remove — not add/remove, which are for subjects Palmer searches news for. All three kinds are on by default. Different from set_reminder — these repeat every day.",
        "input_schema": {
            "type": "object",
            "properties": {
                "add": {"type": "array", "items": {"type": "string"}, "description": "Topics to add, e.g. ['Bitcoin price', 'St. Louis weather']"},
                "remove": {"type": "array", "items": {"type": "string"}, "description": "Topics to remove"},
                "enabled": {"type": "boolean", "description": "Set false to pause morning briefings, true to resume. Topics are preserved."},
            "opening_add": {
                "type": "array",
                "items": {"type": "string", "enum": ["restaurants", "events", "movies"]},
                "description": "Kinds of local Opening content to START including: 'restaurants' (new places, bars, food), 'events' (concerts, festivals, live shows), 'movies' (films and series out this week). Use for 'I want movie openings too', 'add concerts', 'tell me about new restaurants'. All three are on by default, so only pass this to turn something back on after they removed it."},
            "episode_alerts": {
                "type": "boolean",
                "description": "Set true when they ask to be TOLD about new episodes in the morning text ('text me when a new episode is out'), false to stop. Followed shows always appear on their page; this controls whether they also get mentioned in the morning message, which is off by default."},
            "opening_remove": {
                "type": "array",
                "items": {"type": "string", "enum": ["restaurants", "events", "movies"]},
                "description": "Kinds of Opening content to STOP including. Use for 'no more concerts', 'drop the movie stuff', 'I don't care about restaurants'. Removing all three switches the section off entirely."},
            },
            "required": [],
        },
    },
    {
        "name": "set_morning_time",
        "description": "Change what time the user's daily morning briefing is sent, in their local timezone. Use when they ask to get their morning update earlier, later, or at a specific time (e.g. 'send my update at 8', 'make my briefing 9am'). The default is 07:00. Convert whatever they say into 24-hour HH:MM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time": {"type": "string", "description": "24-hour local time HH:MM, e.g. '07:00' for 7am, '08:30' for 8:30am, '09:15' for 9:15am"},
            },
            "required": ["time"],
        },
    },
    {
        "name": "cancel_reminders",
        "description": "Cancel pending reminders that haven't fired yet, including recurring ones — cancelling a repeating reminder stops it for good, it does not just skip the next one. If text_match is given, cancels only reminders whose text contains that phrase. If omitted, cancels all pending reminders for this user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text_match": {"type": "string", "description": "Optional: only cancel reminders matching this phrase. Omit to cancel all pending reminders."},
            },
            "required": [],
        },
    },
    {
        "name": "add_watch",
        "description": "Set up a persistent background news watch. Palmer checks every 30 minutes and only texts the user when something major breaks — corroborated across trusted sources, genuinely new and time-sensitive. Routine coverage, analysis, and incremental updates never fire. Trigger on intent, not vocabulary: any time the user shows they want to stay informed about something evolving over time — a story, a market, a team, a person, an unfolding situation — call this tool. Intent can arrive as a direct ask ('track Iran for me'), an indirect one ('I want to see how this plays out'), a casual aside ('let me know if anything wild happens with the Fed'), or embedded in a longer message. If you're genuinely unsure whether it's a tracking request, ask; if it plausibly is, just set the watch — the user can cancel. Works for geopolitics, sports, stocks and crypto, elections, product launches, science, anything news-trackable. Generate 2-3 specific, targeted search queries that will surface the event if it occurs. When confirming with the user, tell them plainly: you'll only text if something major actually breaks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What to watch for, in the user's own terms. e.g. 'Iran and US military strikes'"},
                "queries": {"type": "array", "items": {"type": "string"}, "description": "2-3 search queries to run every 30 min. Make them specific enough to hit on the event but not so narrow they miss variations. e.g. ['Iran US military strike attack 2026', 'US strikes Iran retaliation']"},
                "cooldown_hours": {"type": "integer", "description": "Minimum hours between alerts for this watch. Default 4. Use 1-2 for urgent breaking-news watches, 8-12 for slower-moving situations."},
            },
            "required": ["description", "queries"],
        },
    },
    {
        "name": "cancel_watch",
        "description": "Cancel one or all active background news watches. Use when the user says 'stop watching', 'I don't need that alert anymore', etc. This is for news/event watches — use cancel_price_watch for product price watches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text_match": {"type": "string", "description": "Optional: cancel only watches whose description contains this phrase. Omit to cancel all watches."},
            },
            "required": [],
        },
    },
    {
        "name": "search_shopping",
        "description": "Search Google Shopping for products RIGHT NOW — a one-shot browse answer, not a persistent watch. Use when the user is discovering, browsing, gift-hunting, or asking what's available at a price point ('show me Reebok shoes around $100', 'good waterproof headphones under $150', 'find me a wool coat', 'gift ideas for a 12 year old under $50'). Extract a clean query (brand + category + any qualifiers the user mentioned). If they gave a hard cap ('under $150'), pass it as max_price. If they said 'around $100', pass min_price/max_price as a reasonable band (roughly ±30%).\n\nTWO MODES:\n\n1. Browse mode (default, include_link=false): returns cheapest-first list of price/title/merchant only, no URLs. Use for the initial recommendation. In your reply, pick 2-3 you'd actually recommend and describe them in your own voice — no bullets, no dump, no URLs.\n\n2. Link mode (include_link=true): returns exactly ONE row for the top match, ending in ' | <direct merchant URL>' that goes directly to the merchant site (not Google's aggregator). Use this the moment the user asks for a link, where to buy, or a purchase URL for a specific product ('send me a link to the Rockaway', 'where can I buy it', 'link to the second one'). Formulate a NARROW query targeting just that product (e.g. 'Madewell Rockaway Tee'). Send only that URL in your reply — no other links.\n\nThis is separate from add_price_watch (persistent) and get_price (crypto/stock only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Clean product query. e.g. 'Reebok mens running shoes', 'waterproof over-ear headphones', 'merino wool sweater womens'. In link mode, narrow to the specific product ('Madewell Rockaway Tee')."},
                "max_price": {"type": "number", "description": "Optional upper price cap in USD."},
                "min_price": {"type": "number", "description": "Optional lower price floor in USD."},
                "include_link": {"type": "boolean", "description": "Default false. Set true when the user explicitly asks for a link, where to buy, or a purchase URL. Returns top match with direct merchant URL."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "browse_shop",
        "description": "Return ONE clean URL to a brand or retailer page the user can open and browse themselves. Use when the user wants to land on a store's category or brand page ('send me the Madewell men's tees page', 'link me to Nike running shoes', 'where do I browse Reformation dresses'). This is different from search_shopping: use search_shopping when the user wants YOU to pull specific product options and describe them; use browse_shop when they want to open the site and browse. Include brand + category in the query ('Madewell mens tee shirts', 'Nike running shoes womens'). Never invent URLs — always call this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Brand + category, e.g. 'Madewell mens tee shirts', 'Nike running shoes womens', 'Reformation dresses'."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_flights",
        "description": "Search Google Flights for a route and date. Use when the user asks about flight prices, options, or availability ('flights BOS to LAX Nov 15-20', 'one way to Lisbon next Friday', 'how much is Chicago to Tokyo in March'). Returns cheapest-first summaries with price, airline, stops, dep/arr times, and total duration — round-trip prices are totals. Extract IATA airport codes (Boston→BOS, LA→LAX, Tokyo→NRT/HND; pick the primary international airport if the user names a city) and dates as YYYY-MM-DD, resolving relative dates against today. Omit return_date for one-way. In your reply, pick the 1-2 options that actually matter (cheapest, or the best schedule/nonstop tradeoff) and describe them in prose — no bullets, no URLs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "IATA airport code, e.g. 'BOS', 'LAX', 'JFK'. Convert city names to the primary international airport."},
                "destination": {"type": "string", "description": "IATA airport code."},
                "outbound_date": {"type": "string", "description": "Departure date, YYYY-MM-DD. Resolve relative dates ('next Friday', 'March 15') against today."},
                "return_date": {"type": "string", "description": "Optional return date, YYYY-MM-DD. Omit for one-way."},
            },
            "required": ["origin", "destination", "outbound_date"],
        },
    },
    {
        "name": "search_hotels",
        "description": "Search Google Hotels for a location and date range. Use when the user asks about hotel prices, availability, or options ('hotels in Lisbon Nov 15-20', 'somewhere in Shoreditch next weekend under $200'). Returns cheapest-first summaries with per-night price, name, rating, review count, and star class. Extract location as you'd search on Google Maps — city or 'neighborhood, city' when disambiguating ('Shoreditch, London', 'Times Square, New York'). Dates as YYYY-MM-DD; resolve relative dates against today. Pass max_price if they gave a per-night cap. Pass min_rating (3.5, 4.0, or 4.5 — other values snap down) if they said 'nice', 'well reviewed', or similar. In your reply, pick 1-2 that actually matter and describe them in prose — no bullets, no URLs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Where to search. City or 'neighborhood, city', e.g. 'Lisbon', 'Shoreditch, London', 'Times Square, New York'."},
                "check_in_date": {"type": "string", "description": "Check-in date, YYYY-MM-DD."},
                "check_out_date": {"type": "string", "description": "Check-out date, YYYY-MM-DD."},
                "max_price": {"type": "number", "description": "Optional per-night price cap in USD."},
                "min_rating": {"type": "number", "description": "Optional rating floor. Only 3.5, 4.0, or 4.5 are supported; other values snap down."},
            },
            "required": ["location", "check_in_date", "check_out_date"],
        },
    },
    {
        "name": "add_price_watch",
        "description": "Start tracking a product's price via Google Shopping (cheapest across merchants: Target, Nordstrom, Best Buy, etc.). Palmer checks twice a day and texts when the price hits the user's target OR moves more than $2 in either direction from the last price he told them about. Rises included — a jump is the signal to buy now rather than wait. Use for general product tracking. If the user is clearly on Amazon or names a category where Amazon is the obvious channel (supplements, protein, coffee, household staples), use add_amazon_watch instead. Extract a clean product_name (brand + model, size/color if they specified). If they gave a target price, pass it as target_price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "Clean product query, e.g. 'Nike Pegasus 40 men's', 'Sony WH-1000XM5 headphones', 'Lululemon Align 25\" leggings'. Keep brand + model. Include size/color only if the user specified."},
                "target_price": {"type": "number", "description": "Optional target price in currency units. Alert fires when current price is at or below this. Pass it whenever the user names a number in any form ('under $40', 'if it gets to 35', 'below fifty bucks') — a target is what makes the watch fire on their terms rather than on the generic drop bar. Omit only if they truly named none."},
                "currency": {"type": "string", "description": "Currency code, default 'USD'. Only override if the user is clearly outside the US."},
            },
            "required": ["product_name"],
        },
    },
    {
        "name": "add_amazon_watch",
        "description": "Watch ONE specific Amazon listing for price drops. Use when the user says 'on Amazon', mentions Prime, or names a category where Amazon is the obvious channel (supplements, protein powder, coffee, paper goods, household staples that swing hard on Amazon). Palmer resolves the item to an Amazon ASIN and tracks that exact listing across ticks — so alerts stay pinned to the right seller and pack size. Palmer checks twice a day and texts when the price hits the user's target OR moves more than $2 in either direction from the last price he told them about. Rises included — a jump is the signal to buy now rather than wait. Distinct from add_price_watch, which searches Google Shopping across many merchants. Cancel via cancel_price_watch — one id space.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string", "description": "The item description in the user's words ('Optimum Nutrition Gold Standard whey chocolate 5lb', 'Kirkland fish oil 400 count'), OR a raw Amazon URL (amazon.com/dp/… or a.co/d/… / amzn.to/… shortener) — Palmer will resolve URLs to the exact listing directly. Keep brand + variant/size/flavor when the user gave them."},
                "target_price": {"type": "number", "description": "Optional target price in USD. Alert fires when current price is at or below this. Pass it whenever the user names a number in any form ('under $40', 'if it gets to 35', 'below fifty bucks') — a target is what makes the watch fire on their terms rather than on the generic drop bar. Omit only if they truly named none."},
            },
            "required": ["product_query"],
        },
    },
    {
        "name": "cancel_price_watch",
        "description": "Cancel one or all active product price watches (works for both Google Shopping and Amazon watches — one id space). Use when the user says 'stop watching those shoes', 'I bought them already', 'kill the Kindle watch', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text_match": {"type": "string", "description": "Optional: cancel only price watches whose product name contains this phrase. Omit to cancel all price watches."},
            },
            "required": [],
        },
    },
    {
        "name": "list_watches",
        "description": "Return the caller's current active watches — both news watches (add_watch) and price watches (add_price_watch / add_amazon_watch). Call this whenever the user asks what you're watching, tracking, keeping tabs on, or 'have set up' ('what are you watching for me', 'list my watches', 'what am I tracking', 'do I have any price watches'). Prefer this over recalling from context — the tool result is authoritative. Weave the result naturally into your reply; do NOT dump the raw list.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_my_page",
        "description": "Return the live URL of THIS user's own page — the same one that goes out with their morning update. It holds their weather, commute, the prices they follow, their headlines, and everything you're watching for them, and it refreshes itself. Call this whenever they ask for it in any words: 'send me my page', 'what's that link', 'link me my dashboard', 'can I see my stuff', 'resend this morning's link', 'where's my briefing'. Also call it when they ask for a rundown of everything at once ('catch me up', 'what's going on today') and the page would answer better than a wall of text — but not for a single specific question, where the right tool is the specific one. The URL is stable and permanent, so resending it is always safe.\n\nThe tool result contains the URL. Put it at the very END of your reply with nothing after it, exactly as returned — no shortening, no markdown, no parentheses around it. Message apps only draw the rich preview when the link sits at a boundary, and that preview is the point. Say one short thing in your own voice and let the link close the message.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_travel_time",
        "description": "Get driving time between two addresses using live traffic. Call this whenever the user asks how long a drive takes, when to leave, or ETA to a place ('how long to Fenway?', 'when should I leave for the airport?', 'time to my sister's from here?'). If the user names a destination but not an origin (or vice versa), ask them conversationally for the missing one in your own voice BEFORE calling this tool. We don't store addresses; ask fresh each time unless the user provides both in the same message.\n\nCRITICAL: Our geocoder is a mapping service, not a search engine — it does NOT reliably resolve famous landmarks, monuments, or business names. If the user gives a landmark, POI, monument, park, stadium, airport, or well-known building (e.g. 'The White House', 'Fenway Park', 'LAX', 'Times Square', 'the Golden Gate Bridge', 'Central Park', 'Wrigley Field'), YOU must convert it to the actual street address using your world knowledge before calling — pass '1600 Pennsylvania Ave NW, Washington DC 20500' not 'The White House'; pass '4 Jersey St, Boston MA 02215' not 'Fenway Park'; pass '1 World Way, Los Angeles CA 90045' not 'LAX'. Only pass raw landmark names as a last resort when you genuinely don't know the address (in which case ask the user for one). Never guess an address you don't actually know.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Starting street address. Convert landmarks to street addresses first (see tool description). Example: '123 Beacon St, Boston MA 02116'."},
                "destination": {"type": "string", "description": "Ending street address. Convert landmarks to street addresses first (see tool description). Example: '1600 Pennsylvania Ave NW, Washington DC 20500'."},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "get_city_traffic",
        "description": "Get a general traffic snapshot for a city — overall flow plus notable incidents (accidents, jams, closures). Call this whenever the user asks about traffic in a place without giving a specific route ('how's traffic in Culver City?', 'roads bad around Boston right now?', 'any accidents downtown?'). If the user says 'here', 'my city', or doesn't specify a place, use their saved city from their profile. This is different from get_travel_time — use that only when the user has (or wants to give) both an origin and destination.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name (e.g. 'St. Louis, MO', 'Culver City, CA'). Include state/country when helpful for disambiguation."},
            },
            "required": ["city"],
        },
    },
    {
        "name": "add_flight_watch",
        "description": "Watch a flight route for price changes and text them when the fare moves. Use when they want ONGOING tracking — 'track flight prices LAX to Milan', 'let me know if that fare drops', 'watch this route'. For a one-off 'how much is X to Y right now', use search_flights instead. Checked once a day; they are alerted on a target hit or any move over $40. Max 3 active routes per person.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "IATA airport code, e.g. 'LAX'"},
                "destination": {"type": "string", "description": "IATA airport code, e.g. 'MXP'"},
                "outbound_date": {"type": "string", "description": "YYYY-MM-DD"},
                "return_date": {"type": "string", "description": "YYYY-MM-DD. Omit for one-way."},
                "target_price": {"type": "number", "description": "Alert immediately at or below this total. Optional."},
            },
            "required": ["origin", "destination", "outbound_date"],
        },
    },
    {
        "name": "cancel_flight_watch",
        "description": "Stop watching a flight route. Pass text_match with an airport code or city to cancel one route; omit it to cancel all of their flight watches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text_match": {"type": "string", "description": "Airport code or city to match, e.g. 'LAX'. Omit to cancel all."},
            },
            "required": [],
        },
    },
    {
        "name": "follow_show",
        "description": "Follow a TV series so they hear when a new episode lands. Use when someone says they watch something and wants to keep up — 'track Reacher for me', 'I watch Silo, tell me when new ones drop', 'follow The Bear'. The show appears on their page in the week an episode airs and is quiet between seasons. This is NOT for a one-off question about a show, and NOT the same as update_morning_briefing, which is for news subjects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The series title as they said it, e.g. 'Reacher'. It is resolved for you."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "unfollow_show",
        "description": "Stop following a TV series. Pass text_match with part of the title to drop one; omit it to drop all of them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text_match": {"type": "string", "description": "Part of the show title, e.g. 'Reacher'. Omit to unfollow everything."},
            },
            "required": [],
        },
    },
    {
        "name": "follow_team",
        "description": "Follow a sports team for live score alerts. Use when they say they want score updates — 'follow the Eagles', 'text me Cardinals scores', 'track the Blues'. They get a text when the lead changes, when someone scores in the last five minutes, and at the final — a few a game, not every play. Team names are ambiguous ('Cardinals' is two teams, 'Rangers' is two), so if the result comes back with more than one match, ASK which they mean before following.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Team as they said it, e.g. 'Eagles'. Add the sport or city if they gave one, e.g. 'St. Louis Cardinals'."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "unfollow_team",
        "description": "Stop live score alerts for a team. Pass text_match with part of the team name; omit to stop all of them.",
        "input_schema": {
            "type": "object",
            "properties": {"text_match": {"type": "string", "description": "Part of the team name. Omit to unfollow all."}},
            "required": [],
        },
    },
    {
        "name": "get_score",
        "description": "Live or final score for a team's game today. Use for 'what's the Eagles score', 'are the Cardinals winning', 'did the Blues win'. This is a one-off lookup and does NOT start alerts — that is follow_team.",
        "input_schema": {
            "type": "object",
            "properties": {"team": {"type": "string", "description": "Team name, e.g. 'Eagles'"}},
            "required": ["team"],
        },
    },
]
