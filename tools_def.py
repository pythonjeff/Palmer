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
        "description": "Get accurate weather — current conditions or multi-day forecast. Use for ANY weather question. Pass the user's city from their profile if they don't specify a location.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name, e.g. 'Chicago' or 'New York'"},
                "when": {"type": "string", "description": "When: 'now', 'today', 'tomorrow', 'this weekend', 'next saturday', or a date like '2026-08-02'. Defaults to today."},
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_price",
        "description": "Get real-time price for crypto or stocks. Use for Bitcoin, Ethereum, other crypto, or any stock ticker (AAPL, TSLA, SPY, QQQ, etc). Returns current price and % change.",
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
        "description": "Save a reminder for the user to be sent at a future time. Call this whenever the user asks to be reminded about something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind the user about"},
                "due_at": {"type": "string", "description": "ISO 8601 UTC datetime when to send the reminder (e.g. 2026-07-21T20:00:00Z)"},
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
        "description": "Add or remove topics from the user's daily morning briefing, or pause/resume it entirely. Use when the user asks to track something every morning (e.g. 'add Bitcoin to my morning', 'stop sending me sports'), or says 'stop my morning' / 'pause mornings' (enabled=false) / 'resume my morning' (enabled=true). Topics are preserved when paused. This is different from set_reminder — morning topics repeat every day.",
        "input_schema": {
            "type": "object",
            "properties": {
                "add": {"type": "array", "items": {"type": "string"}, "description": "Topics to add, e.g. ['Bitcoin price', 'St. Louis weather']"},
                "remove": {"type": "array", "items": {"type": "string"}, "description": "Topics to remove"},
                "enabled": {"type": "boolean", "description": "Set false to pause morning briefings, true to resume. Topics are preserved."},
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
        "description": "Cancel pending reminders that haven't fired yet. If text_match is given, cancels only reminders whose text contains that phrase. If omitted, cancels all pending reminders for this user.",
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
        "description": "Start tracking a product's price via Google Shopping (cheapest across merchants: Target, Nordstrom, Best Buy, etc.). Palmer checks every 12 hours and texts when the price hits the user's target OR drops at least 15% from the price at watch creation. Use for general product tracking. If the user is clearly on Amazon or names a category where Amazon is the obvious channel (supplements, protein, coffee, household staples), use add_amazon_watch instead. Extract a clean product_name (brand + model, size/color if they specified). If they gave a target price, pass it as target_price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "Clean product query, e.g. 'Nike Pegasus 40 men's', 'Sony WH-1000XM5 headphones', 'Lululemon Align 25\" leggings'. Keep brand + model. Include size/color only if the user specified."},
                "target_price": {"type": "number", "description": "Optional target price in currency units. Alert fires when current price is at or below this. Omit if the user didn't name a target."},
                "currency": {"type": "string", "description": "Currency code, default 'USD'. Only override if the user is clearly outside the US."},
            },
            "required": ["product_name"],
        },
    },
    {
        "name": "add_amazon_watch",
        "description": "Watch ONE specific Amazon listing for price drops. Use when the user says 'on Amazon', mentions Prime, or names a category where Amazon is the obvious channel (supplements, protein powder, coffee, paper goods, household staples that swing hard on Amazon). Palmer resolves the item to an Amazon ASIN and tracks that exact listing across ticks — so alerts stay pinned to the right seller and pack size. Palmer checks every 12 hours and texts when the price hits the user's target OR drops at least 15% from the baseline. Distinct from add_price_watch, which searches Google Shopping across many merchants. Cancel via cancel_price_watch — one id space.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string", "description": "The item description in the user's words ('Optimum Nutrition Gold Standard whey chocolate 5lb', 'Kirkland fish oil 400 count'), OR a raw Amazon URL (amazon.com/dp/… or a.co/d/… / amzn.to/… shortener) — Palmer will resolve URLs to the exact listing directly. Keep brand + variant/size/flavor when the user gave them."},
                "target_price": {"type": "number", "description": "Optional target price in USD. Alert fires when current price is at or below this. Omit if the user didn't name a target."},
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
]
