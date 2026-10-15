# Design notes

One file per area of Palmer. Each collects the decisions behind that area and the incident that forced each one, kept verbatim from when they were made. `CLAUDE.md` at the repo root is the short operational guide and points here.

- [agent-loop.md](agent-loop.md) — How a reply is made: the system prompt, the tool loop, dispatch, and routing
- [guards.md](guards.md) — Rules the prompt states and the model breaks, enforced in code
- [db.md](db.md) — The database layer
- [scheduler.md](scheduler.md) — The single process and its jobs
- [reminders.md](reminders.md) — Reminders and recurrence
- [morning.md](morning.md) — The morning send
- [onboarding.md](onboarding.md) — How a new user is set up
- [checkin.md](checkin.md) — The one text on Palmer's own initiative
- [sms.md](sms.md) — Inbound and outbound SMS
- [opening.md](opening.md) — What is newly open nearby
- [scores-and-shows.md](scores-and-shows.md) — Followed teams and followed shows
- [page.md](page.md) — Palmer Home and its preview card
- [profile.md](profile.md) — What Palmer remembers about a person
- [sources.md](sources.md) — News source quality
- [weather.md](weather.md) — Forecasts
- [commute.md](commute.md) — Traffic and the commute
- [time.md](time.md) — Clocks and calendars
- [shopping-and-flights.md](shopping-and-flights.md) — Price watches and flight watches
