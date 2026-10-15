# Scheduler

The single process and its jobs. Governs `main.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Single-process is a hard requirement

`WEB_CONCURRENCY=1` must be set in production. `main.py` holds all cross-request coordination in memory: per-phone `threading.Lock` (`_phone_locks`) serializes inbound messages so conversation history never interleaves, `_in_flight` tracks concurrent turns from the same number so replies can be quote-prefixed, and `_seen_sids` deduplicates Twilio webhook retries. APScheduler also runs in-process. A second worker breaks all of the above.
