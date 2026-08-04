# Architecture V2 — Bot 1 Removed, Mongo Dedup Gate

**Status:** design locked 2026-08-04.
**Scope:** replaces Bot 1 (@postedstuffbot / @hentaifoxbot) with an in-house
cover-poster and enforces a MongoDB-backed dedup gate keyed on `gallery_id`.

---

## 1. Old flow (V1)

```
User → admin_bot / miniapp
        └─▶ queue_service.enqueue_batch
                └─▶ worker._run_loop
                        └─▶ relay.process_job
                              ├─ DM URL → Bot 1 (@hentaifoxbot)
                              ├─ DM URL → Bot 2 (@Gallery_DLBot)
                              ├─ wait for Bot 1 cover in DB channel   (slug match)
                              ├─ wait for Bot 2 PDF in DM
                              ├─ forward PDF into DB channel under cover
                              └─ record_processed(url, url_hash)
```

Dedup was `processed_urls[url_hash]` only. Same gallery submitted via a
different URL slug (e.g. `nhentai.net/g/304307` vs `nhentai.net/g/304307/`)
would slip past.

---

## 2. New flow (V2)

```
User → miniapp / admin_bot
        └─▶ queue_service.enqueue_batch
                ├─ dedup gate (Mongo galleries[gallery_id])
                │     COMPLETED   → return open_post_link (no work)
                │     PROCESSING  → return "already downloading"
                │     stale >15m  → treat as new, reset doc
                │     new         → insert {status: PROCESSING}, enqueue job
                └─▶ worker._run_loop
                        └─▶ relay.process_job  (V2 orchestrator)
                              ├─ scrape gallery meta   (hf_scraper)
                              ├─ post cover+caption to DB channel (in-house)
                              ├─ DM URL → Bot 2 (@Gallery_DLBot)
                              ├─ wait for Bot 2 PDF in DM
                              │     text reply         → FAILED_BOT2_ERROR
                              │                          delete cover, purge doc,
                              │                          notify admin,
                              │                          tell user "select another"
                              │     no reply (8 min)   → FAILED_TIMEOUT tombstone
                              │     pdf                → forward as reply to cover
                              │                          (drop_author=true)
                              ├─ galleries[gid].status = COMPLETED
                              │     + db_cover_msg_id, db_pdf_msg_id, open_link
                              └─ deliver cover + PDF back to requester
```

---

## 3. Mongo collections

### 3.1 `galleries` (NEW)

Primary source of truth for dedup + delivery.

```
{
  "_id"              : "304307",              # gallery_id (string form)
  "gallery_id"       : "304307",
  "url"              : "https://nhentai.net/g/304307/",
  "url_hash"         : "<sha1>",
  "status"           : "PROCESSING" | "COMPLETED" | "PARTIAL"
                       | "FAILED_TIMEOUT" | "FAILED_BOT2_ERROR"
                       | "FAILED_SCRAPE"  | "FAILED_OTHER",
  "title"            : "Akarui Kazoku Seikatsu",
  "pages"            : 65,
  "tags"             : [ { "name": "big breasts", "type": "tag" }, ... ],
  "cover_url"        : "https://t.nhentai.net/galleries/1584515/thumb.jpg",

  "db_cover_msg_id"  : 12345,                 # our own cover post in DB channel
  "db_pdf_msg_id"    : 12346,                 # PDF forwarded under it
  "open_link"        : "https://t.me/c/<internal>/12345",

  "requested_by"     : [ 8679252317, 123456 ],
  "created_at"       : ISODate,
  "started_at"       : ISODate,
  "completed_at"     : ISODate | null,
  "failed_reason"    : "" | "bot2 said: <msg>" | "scrape returned None" | ...,
  "job_id"           : 91234                  # last associated queue job
}
```

Indexes:

- `_id`  (implicit, unique)  — atomic upsert on new arrival
- `status`                    — worker sweeps + admin stats
- `started_at` (partial: status=PROCESSING) — lazy stale-doc detection

### 3.2 Existing collections (unchanged)

- `queue`             — async job pipeline (Bot 2 orchestration + retries)
- `processed_urls`    — legacy `url_hash` dedup (kept for backward compat)
- `settings` / `flags` — bot flags, auto-queue, etc.
- `flood_events`, `bot_pings`, `admins`, `users`, `progress_batches` — unchanged

Rule: **`galleries` is the truth for dedup + delivery. `processed_urls` stays as
a belt-and-suspenders check for URL variants that miss the gallery_id extraction
(hentaifox legacy IDs, etc.).**

---

## 4. State machine

```
                              enqueue
                                 │
                                 ▼
                          ┌──────────────┐
                    ┌──── │ PROCESSING   │ ────┐
                    │     └──────────────┘     │
             scrape fail        │       Bot 2 pdf ok
                    │           │              │
                    ▼           ▼              ▼
             FAILED_SCRAPE    Bot 2         COMPLETED
                            text | ∅
                              │
                              ▼
                       FAILED_BOT2_ERROR
                     (cover post deleted,
                      doc purged, admin
                      notified)
                            OR
                       FAILED_TIMEOUT
                     (tombstone kept for
                      lazy recovery)
```

- Stale detection: any doc with `status == "PROCESSING"` older than
  `MINIAPP_STALE_PROCESSING_S` (default 900) is treated as “no longer being
  worked” by the dedup gate; it is reset to a fresh PROCESSING doc and
  re-enqueued.
- FAILED_TIMEOUT / FAILED_SCRAPE keep a tombstone so retries by users hit an
  informative error. `/admin_bot resetdoc <gid>` (new command, Phase 75%)
  lets an admin clear one.
- FAILED_BOT2_ERROR purges the doc so the user can retry cleanly after Bot 2
  fixes the source; admin is notified in DM with the offending link.

---

## 5. Detection of Bot 2's PDF (confirmed against screenshot)

- UserBot session DMs the gallery link to `@Gallery_DLBot`.
- Bot 2 replies in the SAME DM with a `Document` where
  `mime_type == 'application/pdf'`. Screenshot samples:
    - `Muramata-san no Himitsu.pdf`   41.2 MB
    - `Kyoudai ni Okeru Seikoushou no Kiroku.pdf`  14.1 MB
    - `Kko to Yamioji Ha.pdf`  37.6 MB
- Filename matches the gallery title (with `.pdf`); caption = title.
- Detection strategy: Telethon `iter_messages` on the Bot 2 dialog, filtering
  to `msg.date >= since_ts` AND `msg.document.mime_type == 'application/pdf'`.
  (Same shape as V1's `_wait_bot2_pdf`, kept as-is.)
- Text reply → `FAILED_BOT2_ERROR`.
- Nothing within `BOT2_PDF_TIMEOUT_SEC` (default 480 = 8 min) → `FAILED_TIMEOUT`.
- Forward is `client.forward_messages(channel, bot2_msg, drop_author=True)` —
  exactly the V1 mechanism (removes the "Forwarded from @Gallery_DLBot" tag).

---

## 6. Config additions

| Env var                          | Default        | Purpose                                            |
| -------------------------------- | -------------- | -------------------------------------------------- |
| `BOT2_USERNAME`                  | @Gallery_DLBot | (existing) target for PDF fetch                    |
| `BOT2_PDF_TIMEOUT_SEC`           | 480            | (existing) 8-minute Bot 2 wait                     |
| `BOT1_USERNAME`                  | *(deprecated)* | soft-warn if set; unused in V2                     |
| `MINIAPP_STALE_PROCESSING_S`     | 900            | after 15 min, PROCESSING → recoverable             |
| `SELF_COVER_POST_ENABLED`        | 1              | master switch (rollback safety valve)              |

---

## 7. File touch list

**Modified**
- `db.py`             — new `galleries` helpers (`gallery_get`, `gallery_claim`,
                         `gallery_mark_completed`, `gallery_mark_failed`, ...)
- `relay.py`          — `process_job` rebuilt for V2
- `queue_service.py`  — enqueue gate consults `galleries`
- `worker.py`         — thin: pass through the new outcome codes
- `admin_bot.py`      — `/search` → miniapp button msg; deprecation warn on BOT1
- `config.py`         — new env-vars

**Added**
- `gallery_state.py`  — pure state machine helpers around `galleries` collection
- `cover_poster.py`   — scrape+post cover, delete cover on failure
- `bot2_client.py`    — DM Bot 2, await PDF, timeout, error-text detection
- `docs/ARCHITECTURE_V2.md` (this file)
- `docs/MIGRATION_V2.md`

**Mini-app changes**
- `miniapp/backend/app/routes/queue.py`   — return `already_completed{link}` etc.
- `miniapp/backend/app/routes/gallery.py` — expose per-gallery status
- `miniapp/frontend/js/plugins/card-actions.js` — "Queue" ⇄ "Open Post" swap
- `miniapp/frontend/js/pages/queue.js`    — surface COMPLETED links

---

## 8. Non-goals

- Not changing the queue algorithm (still FIFO, one job at a time).
- Not changing Bot 3 /mpost logic (still fires for non-admin /search jobs).
- Not touching auto-queue scheduler (patched previously).
- Not touching miniapp auth / rate-limit / admin controls.
