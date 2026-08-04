# V2 Migration Guide — Bot 1 Removal + `galleries` collection

This guide is for the maintainer of https://github.com/vairegi/mtproto-userbot
who is upgrading a V1 deployment to V2.

**Downtime target:** none. V2 is designed to run alongside the existing V1
queue rows; the enqueue gate is the only breaking behaviour change and it is
guarded by an env-var toggle (see §5 rollback).

---

## 1. What actually changes on disk (repo)

| Path                                             | Change  |
| ------------------------------------------------ | ------- |
| `db.py`                                          | + `galleries` collection helpers |
| `gallery_state.py`                               | **NEW** state-machine module |
| `cover_poster.py`                                | **NEW** in-house cover post |
| `bot2_client.py`                                 | **NEW** thin Bot 2 helper |
| `relay.py`                                       | `process_job` rebuilt |
| `queue_service.py`                               | enqueue-gate consults `galleries` |
| `worker.py`                                      | outcome mapping updated |
| `admin_bot.py`                                   | `/search` → mini-app button; BOT1 warn |
| `config.py`                                      | + `MINIAPP_STALE_PROCESSING_S`, `SELF_COVER_POST_ENABLED` |
| `miniapp/backend/app/routes/queue.py`            | dedup response shape |
| `miniapp/backend/app/routes/gallery.py`          | + `/status` |
| `miniapp/frontend/js/plugins/card-actions.js`    | Queue ⇄ Open Post swap |
| `miniapp/frontend/js/pages/queue.js`             | show COMPLETED deep-links |
| `docs/ARCHITECTURE_V2.md`, `docs/MIGRATION_V2.md`| **NEW** |

## 2. What changes in MongoDB Atlas

Nothing to drop, nothing to migrate manually. On the first V2 boot,
`db._ensure_indexes` will create the new `galleries` collection and its
indexes lazily:

```
galleries._id                (unique, implicit)
galleries.status             — ascending
galleries.started_at         — ascending, partialFilterExpression status=PROCESSING
```

The legacy `processed_urls` collection is untouched and continues to serve as
a secondary belt-and-suspenders dedup for URL variants that never yield a
`gallery_id` (rare, e.g. malformed hentaifox links).

## 3. Environment variables to set on Render

| Var                              | Recommended value | Notes |
| -------------------------------- | ----------------- | ----- |
| `MINIAPP_STALE_PROCESSING_S`     | `900`             | 15 min lazy timeout |
| `SELF_COVER_POST_ENABLED`        | `1`               | set `0` to fall back to V1 for one deploy |
| `BOT2_USERNAME`                  | `@Gallery_DLBot`  | unchanged |
| `BOT2_PDF_TIMEOUT_SEC`           | `480`             | unchanged |

Deprecated but tolerated (a warning is logged if present):

| Var                              | Fate |
| -------------------------------- | ---- |
| `BOT1_USERNAME`                  | ignored; will be removed in V3 |

## 4. Deploy sequence (zero-downtime)

1. Merge the V2 branch, push to `main`.
2. Set `SELF_COVER_POST_ENABLED=0` on Render **first**, redeploy. V2 code
   is now on disk but the runtime path is still V1 (safety valve).
3. Watch logs for one full request → success. If nothing is broken:
4. Flip `SELF_COVER_POST_ENABLED=1`, redeploy again. V2 is now live.
5. Remove `BOT1_USERNAME` from the Render env once satisfied.

## 5. Rollback

Any of these gets you back to V1 quickly:

- Flip `SELF_COVER_POST_ENABLED=0` — the relay skips the in-house cover
  poster and rejoins the old Bot 1 path (only meaningful if `BOT1_USERNAME`
  is still set; otherwise it returns `PARTIAL`).
- `git revert` the V2 merge commit and redeploy.

The `galleries` collection is additive; leaving it in place is harmless if
you rollback.

## 6. Verification checklist after V2 goes live

- Enqueue a brand-new gallery → single cover post in DB channel authored by
  the userbot session (not Bot 1) → PDF forwarded under it → COMPLETED.
- Enqueue the same gallery again from a different Telegram account → gets
  the "already have it, here you go" branch → no new post in DB channel.
- Enqueue while it's still downloading from a different account → gets
  "already downloading" branch.
- Send a link Bot 2 can't handle → cover post is deleted, admin gets a DM
  with the offending link, user gets "please pick another".
- Open the mini-app Queue tab → COMPLETED rows have "Open Post" instead of
  "Queue".
