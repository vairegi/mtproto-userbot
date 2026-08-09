"""
db.py — MongoDB access layer (drop-in replacement for the old SQLite version).

WHY THIS FILE CHANGED
---------------------
The bot used to store everything in a local file called `queue.db` (SQLite).
On serverless / free hosting (Hugging Face Spaces, Render, Railway, Fly.io...)
the container's local disk is wiped every time the app restarts or redeploys,
so `queue.db` — and with it the queue, admins, tokens and dedupe history —
would silently vanish.

This version stores the exact same data in MongoDB (e.g. a free MongoDB Atlas
cluster), which lives outside the container and therefore survives restarts.

DESIGN NOTE — 100% API COMPATIBLE
---------------------------------
Every public function keeps the SAME name and the SAME arguments as before,
including the leading `conn` parameter. That means the rest of the project
(worker.py, relay.py, admin_bot.py, progress_tracker.py, search_picker.py,
queue_service.py) calls this module exactly as it did with SQLite.

Two small but important compatibility details:

  * `db.connect()` no longer opens a real new connection. It returns a very
    light handle over ONE shared, process-wide MongoClient. `conn.close()` is
    therefore a deliberate no-op. This matters: the old code called
    `db.connect()` / `conn.close()` many times per second (the progress
    tracker polls every 2s), and opening a genuine TCP+TLS connection to
    Atlas that often would be slow and would blow through the free tier's
    connection limit.

  * Rows are returned as plain Python `dict`s, not `sqlite3.Row`. A dict
    supports `row["url"]`, `row.keys()`, `"x" in row.keys()` and `dict(row)`,
    which is every access pattern the rest of the codebase uses.

COLLECTIONS (the MongoDB equivalent of the old tables)
------------------------------------------------------
  queue             : _id(int), url, url_hash, status, created_at, updated_at,
                      error_reason, submitted_by, chat_id, requested_tag,
                      cover_link, via_search, username
  processed_urls    : _id = url_hash, url, first_seen_at, completed_at
  flood_events      : at, seconds, context
  bot_pings         : _id = bot name, last_ok
  control_flags     : _id = key, value
  admins            : _id = user_id, is_super, added_by, added_at
  users             : _id = user_id, first_seen_at, blocked
  job_progress      : _id = job_id, title, phase, detail, updated_at
  progress_batches  : _id = batch_id, chat_id, message_id, job_ids, created_at,
                      completed_at
  user_tokens       : _id = user_id, username, used_today, last_reset_date,
                      last_search_at
  counters          : _id = name, seq        (replaces AUTOINCREMENT)

CONFIGURATION
-------------
Set the environment variable MONGO_URI to your Atlas connection string, e.g.
    mongodb+srv://user:pass@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
Optionally set MONGO_DB_NAME (defaults to "relaybot").
"""
from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Kept for backwards compatibility with startup_check.py's version assertion.
SCHEMA_VERSION = 4

DEFAULT_DB_NAME = "relaybot"


def _mongo_uri() -> str:
    """Read MONGO_URI from the environment.

    We read it lazily (inside the function, not at import time) so that
    importing this module never crashes — the error surfaces with a clear
    message only when the database is actually needed.
    """
    uri = (os.getenv("MONGO_URI") or "").strip()
    if not uri:
        raise RuntimeError(
            "MONGO_URI is not set. Add it as an environment variable / secret "
            "(e.g. mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/"
            "?retryWrites=true&w=majority)."
        )
    return uri


def _db_name() -> str:
    return (os.getenv("MONGO_DB_NAME") or "").strip() or DEFAULT_DB_NAME


# ---------------------------------------------------------------------------
# Shared client (one per process, thread-safe, lazily created)
# ---------------------------------------------------------------------------

_client: Optional[MongoClient] = None
_client_pid: Optional[int] = None
_client_lock = threading.Lock()
_indexes_ready = False


def _get_client() -> MongoClient:
    """Return the process-wide MongoClient, creating it on first use.

    Re-created automatically if the process was forked (a MongoClient must
    never be shared across a fork), which is why we track the PID.
    """
    global _client, _client_pid
    pid = os.getpid()
    if _client is not None and _client_pid == pid:
        return _client
    with _client_lock:
        if _client is not None and _client_pid == pid:
            return _client
        _client = MongoClient(
            _mongo_uri(),
            serverSelectionTimeoutMS=15000,
            connectTimeoutMS=15000,
            socketTimeoutMS=30000,
            retryWrites=True,
            maxPoolSize=20,
            tz_aware=False,
            appname="mtproto-relay",
        )
        _client_pid = pid
        return _client


class MongoHandle:
    """Tiny stand-in for the old `sqlite3.Connection` object.

    The rest of the codebase does:
        conn = db.connect()
        try:
            ... db.something(conn, ...)
        finally:
            conn.close()

    So the handle only needs to exist, expose the database, and tolerate
    `.close()`. `.close()` is intentionally a NO-OP because the underlying
    MongoClient is shared for the whole process and must stay open.
    """

    __slots__ = ("db",)

    def __init__(self, database) -> None:
        self.db = database

    # --- convenience collection accessors -------------------------------
    @property
    def queue(self):
        return self.db["queue"]

    @property
    def processed_urls(self):
        return self.db["processed_urls"]

    @property
    def flood_events(self):
        return self.db["flood_events"]

    @property
    def bot_pings(self):
        return self.db["bot_pings"]

    @property
    def control_flags(self):
        return self.db["control_flags"]

    @property
    def admins(self):
        return self.db["admins"]

    @property
    def users(self):
        return self.db["users"]

    @property
    def job_progress(self):
        return self.db["job_progress"]

    @property
    def progress_batches(self):
        return self.db["progress_batches"]

    @property
    def user_tokens(self):
        return self.db["user_tokens"]

    @property
    def counters(self):
        return self.db["counters"]

    @property
    def galleries(self):
        """V2 dedup + delivery collection (see docs/ARCHITECTURE_V2.md).

        _id = gallery_id (string). Never touched by V1 code paths.
        """
        return self.db["galleries"]

    @property
    def nhentai_cache(self):
        """v12.2: long-TTL cache of upstream nhentai responses.

        _id  = cache key (e.g. 'gallery:274788', 'search:incest|popular|1')
        payload = the JSON blob to return verbatim
        expires_at = epoch seconds; TTL index below purges the doc.

        Serving from here means ZERO upstream fetches — which is the
        single biggest lever against '1 user 429s everyone'.
        """
        return self.db["nhentai_cache"]

    @property
    def nhentai_ratelimit(self):
        """v12.2: shared per-endpoint token bucket for nhentai upstream.

        _id = endpoint tag ('search', 'galleries', 'popular', ...)
        tokens        = float, refills at rate_per_sec toward capacity
        capacity      = int (max tokens = docs' anon limit for that endpoint)
        rate_per_sec  = float (capacity / 60)
        updated_at    = last time tokens were refilled (epoch seconds)

        Sized to the OPENAPI 3.1 anon limits at nhentai.net/api/v2/openapi.json.
        Consumed BEFORE every upstream call so we never blow past quota.
        """
        return self.db["nhentai_ratelimit"]

    def close(self) -> None:
        """No-op: the shared MongoClient stays alive for the process."""
        return None

    # Some older/debug code may try `conn.command(...)`; forward it.
    def command(self, *a, **kw):
        return self.db.command(*a, **kw)


def connect() -> MongoHandle:
    """Return a handle to the MongoDB database (see MongoHandle docstring)."""
    handle = MongoHandle(_get_client()[_db_name()])
    _ensure_indexes(handle)
    return handle


@contextmanager
def transaction(conn: MongoHandle) -> Iterator[MongoHandle]:
    """Compatibility shim.

    The old SQLite layer wrapped writes in BEGIN IMMEDIATE / COMMIT. MongoDB
    single-document updates are already atomic, and multi-document
    transactions require a replica set plus extra latency for no benefit here,
    so this simply yields the handle. Kept so any external code that imported
    `db.transaction` still works.
    """
    yield conn


def _ensure_indexes(conn: MongoHandle) -> None:
    """Create the indexes once per process. Safe and cheap to call repeatedly."""
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        conn.queue.create_index([("status", ASCENDING)], name="idx_queue_status")
        conn.queue.create_index([("url_hash", ASCENDING)], name="idx_queue_hash")
        conn.queue.create_index([("updated_at", DESCENDING)], name="idx_queue_updated")
        conn.queue.create_index(
            [("status", ASCENDING), ("_id", ASCENDING)], name="idx_queue_status_id"
        )
        conn.processed_urls.create_index(
            [("completed_at", ASCENDING)], name="idx_processed_completed"
        )
        conn.progress_batches.create_index(
            [("completed_at", ASCENDING)], name="idx_batches_open"
        )
        conn.user_tokens.create_index([("username", ASCENDING)], name="idx_tokens_username")
        # V2 galleries indexes -------------------------------------------------
        # _id is already unique (implicit). We index status for admin sweeps,
        # and started_at (partial: PROCESSING only) for lazy stale-recovery.
        conn.galleries.create_index(
            [("status", ASCENDING)], name="idx_galleries_status"
        )
        try:
            conn.galleries.create_index(
                [("started_at", ASCENDING)],
                name="idx_galleries_started_processing",
                partialFilterExpression={"status": "PROCESSING"},
            )
        except PyMongoError:
            # Partial index syntax is Mongo 3.2+; if the cluster refuses it,
            # fall back to a plain index so the collection still works.
            conn.galleries.create_index(
                [("started_at", ASCENDING)], name="idx_galleries_started"
            )
        conn.galleries.create_index(
            [("url_hash", ASCENDING)], name="idx_galleries_url_hash"
        )
        # v12.2 nhentai_cache: TTL index on expires_at auto-purges stale docs.
        # expireAfterSeconds=0 means "expire exactly at the datetime in this field".
        try:
            conn.nhentai_cache.create_index(
                [("expires_at", ASCENDING)],
                name="idx_nhcache_ttl",
                expireAfterSeconds=0,
            )
        except PyMongoError:
            # Older Mongo without TTL support — lazy expiry in read path still works.
            pass
        _indexes_ready = True
    except PyMongoError:
        # Index creation is an optimisation, never a hard requirement.
        _indexes_ready = True


def init_db() -> None:
    """Verify connectivity and create indexes. Called at startup."""
    conn = connect()
    # `ping` raises a clear error if MONGO_URI / network / credentials are wrong.
    conn.db.command("ping")
    _ensure_indexes(conn)
    _set_flag_raw(conn, "schema_version", str(SCHEMA_VERSION))


def confirm_wal(conn: MongoHandle) -> str:
    """Compatibility stub.

    SQLite's WAL journal mode has no MongoDB equivalent. startup_check.py used
    to assert this returned 'wal'; it now checks connectivity instead. The
    literal string is returned so any old caller still passes.
    """
    return "wal"


def ping(conn: Optional[MongoHandle] = None) -> bool:
    """True if the MongoDB server answers. Used by the startup self-test."""
    try:
        c = conn or connect()
        c.db.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False


def now_ts() -> int:
    return int(time.time())


def _next_seq(conn: MongoHandle, name: str = "queue") -> int:
    """Atomic auto-increment — replaces SQLite's INTEGER PRIMARY KEY AUTOINCREMENT."""
    doc = conn.counters.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


def _row(doc: Optional[dict], id_field: str = "id") -> Optional[Dict[str, Any]]:
    """Convert a Mongo document to the dict shape the old code expects.

    MongoDB stores the primary key as `_id`; the old SQLite rows exposed it
    under a friendlier name (`id`, `user_id`, `batch_id`, ...). This renames it
    while keeping `_id` available too.
    """
    if doc is None:
        return None
    out = dict(doc)
    if "_id" in out:
        out[id_field] = out["_id"]
    return out


# ---------------------------------------------------------------------------
# queue operations
# ---------------------------------------------------------------------------

_QUEUE_DEFAULTS = {
    "error_reason": None,
    "submitted_by": None,
    "chat_id": None,
    "requested_tag": 0,
    "cover_link": None,
    "via_search": 0,
    "username": None,
}


def _queue_row(doc: Optional[dict]) -> Optional[Dict[str, Any]]:
    """Normalise a queue document so every expected key always exists.

    The old table had columns added over time via ALTER TABLE, so callers use
    guards like `"via_search" in row.keys()`. Filling defaults here means those
    guards behave predictably for documents written by any version.
    """
    row = _row(doc, "id")
    if row is None:
        return None
    for k, v in _QUEUE_DEFAULTS.items():
        row.setdefault(k, v)
    return row


def reset_stuck_processing(conn: MongoHandle) -> int:
    """On restart, any 'processing' job → back to 'pending'."""
    res = conn.queue.update_many(
        {"status": "processing"},
        {"$set": {"status": "pending", "updated_at": now_ts(), "error_reason": None}},
    )
    return int(res.modified_count or 0)


def has_completed(conn: MongoHandle, url_hash: str) -> bool:
    doc = conn.processed_urls.find_one(
        {"_id": url_hash, "completed_at": {"$ne": None}}, {"_id": 1}
    )
    return doc is not None


def has_pending_or_processing(conn: MongoHandle, url_hash: str) -> bool:
    doc = conn.queue.find_one(
        {"url_hash": url_hash, "status": {"$in": ["pending", "processing"]}}, {"_id": 1}
    )
    return doc is not None


def enqueue(
    conn: MongoHandle,
    url: str,
    url_hash: str,
    submitted_by: Optional[int] = None,
    chat_id: Optional[int] = None,
    requested_tag: bool = False,
    via_search: bool = False,
    username: Optional[str] = None,
) -> int:
    ts = now_ts()
    job_id = _next_seq(conn, "queue")
    conn.queue.insert_one(
        {
            "_id": job_id,
            "url": url,
            "url_hash": url_hash,
            "status": "pending",
            "created_at": ts,
            "updated_at": ts,
            "error_reason": None,
            "submitted_by": int(submitted_by) if submitted_by else None,
            "chat_id": int(chat_id) if chat_id else None,
            "requested_tag": 1 if requested_tag else 0,
            "cover_link": None,
            "via_search": 1 if via_search else 0,
            "username": username,
        }
    )
    return int(job_id)


def next_pending(conn: MongoHandle) -> Optional[Dict[str, Any]]:
    doc = conn.queue.find_one({"status": "pending"}, sort=[("_id", ASCENDING)])
    return _queue_row(doc)


def claim_next_pending(conn: MongoHandle) -> Optional[Dict[str, Any]]:
    """Atomically take the oldest pending job and mark it 'processing'.

    NEW (not in the SQLite version). Safe to ignore, but recommended if you
    ever run more than one worker: it makes double-processing impossible
    because the find and the update happen in a single atomic operation.
    """
    doc = conn.queue.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing", "updated_at": now_ts()}},
        sort=[("_id", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    return _queue_row(doc)


def mark_processing(conn: MongoHandle, job_id: int) -> None:
    conn.queue.update_one(
        {"_id": int(job_id)},
        {"$set": {"status": "processing", "updated_at": now_ts()}},
    )


def mark_status(
    conn: MongoHandle,
    job_id: int,
    status: str,
    error_reason: Optional[str] = None,
) -> None:
    conn.queue.update_one(
        {"_id": int(job_id)},
        {"$set": {"status": status, "updated_at": now_ts(), "error_reason": error_reason}},
    )


def set_cover_link(conn: MongoHandle, job_id: int, cover_link: str) -> None:
    """Persist the Database Channel cover-post link (t.me/c/.../msgid) for a job,
    used by the /mpost cross-post step."""
    conn.queue.update_one({"_id": int(job_id)}, {"$set": {"cover_link": cover_link}})


def get_job(conn: MongoHandle, job_id: int) -> Optional[Dict[str, Any]]:
    return _queue_row(conn.queue.find_one({"_id": int(job_id)}))


def record_processed(conn: MongoHandle, url: str, url_hash: str) -> None:
    """Insert the point-of-no-return row the moment PDF is forwarded."""
    ts = now_ts()
    conn.processed_urls.update_one(
        {"_id": url_hash},
        {
            "$set": {"url": url, "completed_at": ts},
            "$setOnInsert": {"first_seen_at": ts},
        },
        upsert=True,
    )


def log_flood(conn: MongoHandle, seconds: int, context: str = "") -> None:
    try:
        conn.flood_events.insert_one(
            {"at": now_ts(), "seconds": int(seconds), "context": (context or "")[:200]}
        )
    except PyMongoError:
        pass  # telemetry only — never break a job over it


def touch_bot_ping(conn: MongoHandle, bot: str) -> None:
    conn.bot_pings.update_one(
        {"_id": bot}, {"$set": {"last_ok": now_ts()}}, upsert=True
    )


def get_bot_ping(conn: MongoHandle, bot: str) -> Optional[int]:
    doc = conn.bot_pings.find_one({"_id": bot})
    return int(doc["last_ok"]) if doc and doc.get("last_ok") is not None else None


# ---------------------------------------------------------------------------
# control flags
# ---------------------------------------------------------------------------

def _set_flag_raw(conn: MongoHandle, key: str, value: str) -> None:
    conn.control_flags.update_one(
        {"_id": key}, {"$set": {"value": str(value)}}, upsert=True
    )


def set_flag(conn: MongoHandle, key: str, value: str) -> None:
    _set_flag_raw(conn, key, value)


def get_flag(conn: MongoHandle, key: str, default: str = "") -> str:
    doc = conn.control_flags.find_one({"_id": key})
    if not doc or doc.get("value") is None:
        return default
    return str(doc["value"])


# ---------------------------------------------------------------------------
# stats / reporting
# ---------------------------------------------------------------------------

def counts_by_status(conn: MongoHandle) -> dict:
    d = {"pending": 0, "processing": 0, "done": 0, "partial": 0, "failed": 0}
    try:
        for row in conn.queue.aggregate(
            [{"$group": {"_id": "$status", "c": {"$sum": 1}}}]
        ):
            status = row.get("_id")
            if status:
                d[str(status)] = int(row.get("c") or 0)
    except PyMongoError:
        pass
    return d


def failed_last_24h(conn: MongoHandle) -> int:
    cutoff = now_ts() - 24 * 3600
    return int(
        conn.queue.count_documents({"status": "failed", "updated_at": {"$gte": cutoff}})
    )


def last_jobs(conn: MongoHandle, n: int = 5) -> list:
    docs = conn.queue.find(
        {},
        {"url": 1, "status": 1, "updated_at": 1, "error_reason": 1},
    ).sort("updated_at", DESCENDING).limit(int(n))
    return [_queue_row(d) for d in docs]


def most_recent_failed(conn: MongoHandle) -> Optional[Dict[str, Any]]:
    doc = conn.queue.find_one({"status": "failed"}, sort=[("updated_at", DESCENDING)])
    return _queue_row(doc)


# ---------------------------------------------------------------------------
# admin management (two-tier)
# ---------------------------------------------------------------------------

def add_admin(
    conn: MongoHandle,
    user_id: int,
    is_super: bool = False,
    added_by: Optional[int] = None,
) -> None:
    conn.admins.update_one(
        {"_id": int(user_id)},
        {
            "$set": {"is_super": 1 if is_super else 0, "added_by": added_by},
            "$setOnInsert": {"added_at": now_ts()},
        },
        upsert=True,
    )


def remove_admin(conn: MongoHandle, user_id: int) -> int:
    res = conn.admins.delete_one({"_id": int(user_id)})
    return int(res.deleted_count or 0)


def get_admin(conn: MongoHandle, user_id: int) -> Optional[Dict[str, Any]]:
    doc = conn.admins.find_one({"_id": int(user_id)})
    row = _row(doc, "user_id")
    if row is not None:
        row.setdefault("is_super", 0)
        row.setdefault("added_by", None)
        row.setdefault("added_at", 0)
    return row


def list_admins(conn: MongoHandle) -> list:
    docs = conn.admins.find({}).sort([("is_super", DESCENDING), ("_id", ASCENDING)])
    out = []
    for d in docs:
        row = _row(d, "user_id")
        row.setdefault("is_super", 0)
        row.setdefault("added_by", None)
        row.setdefault("added_at", 0)
        out.append(row)
    return out


def set_super(conn: MongoHandle, user_id: int, is_super: bool) -> int:
    res = conn.admins.update_one(
        {"_id": int(user_id)}, {"$set": {"is_super": 1 if is_super else 0}}
    )
    return int(res.matched_count or 0)


# ---------------------------------------------------------------------------
# normal users + lock/unlock
# ---------------------------------------------------------------------------

def upsert_user(conn: MongoHandle, user_id: int) -> None:
    conn.users.update_one(
        {"_id": int(user_id)},
        {"$setOnInsert": {"first_seen_at": now_ts(), "blocked": 0}},
        upsert=True,
    )


def is_locked(conn: MongoHandle) -> bool:
    return get_flag(conn, "locked", "0") == "1"


def set_locked(conn: MongoHandle, locked: bool, by_user_id: Optional[int] = None) -> None:
    set_flag(conn, "locked", "1" if locked else "0")
    set_flag(conn, "locked_by", str(by_user_id) if by_user_id is not None else "")
    set_flag(conn, "locked_at", str(now_ts()) if locked else "")


def lock_info(conn: MongoHandle) -> dict:
    return {
        "locked": is_locked(conn),
        "by": get_flag(conn, "locked_by", ""),
        "at": get_flag(conn, "locked_at", ""),
    }


# ---------------------------------------------------------------------------
# job progress tracking (v8)
# ---------------------------------------------------------------------------

PHASE_PENDING = "pending"
PHASE_SENT_BOTS = "sent_bots"
PHASE_WAIT_PDF = "wait_pdf"
PHASE_FORWARDING = "forwarding"
PHASE_MPOSTING = "mposting"
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_PARTIAL = "partial"

_TERMINAL_PHASES = (PHASE_DONE, PHASE_FAILED, PHASE_PARTIAL)


def upsert_job_progress(
    conn: MongoHandle,
    job_id: int,
    phase: str,
    title: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Write/refresh one job's live phase.

    Mirrors the old SQL `title = COALESCE(excluded.title, job_progress.title)`:
    a None title never erases a title that was already stored.
    """
    set_doc: Dict[str, Any] = {
        "phase": phase,
        "detail": detail,
        "updated_at": now_ts(),
    }
    if title is not None:
        set_doc["title"] = title
    conn.job_progress.update_one(
        {"_id": int(job_id)},
        {"$set": set_doc, "$setOnInsert": {"title": title} if title is None else {}},
        upsert=True,
    )


def get_progress_for_jobs(conn: MongoHandle, job_ids) -> list:
    if not job_ids:
        return []
    ids = [int(j) for j in job_ids]
    docs = conn.job_progress.find({"_id": {"$in": ids}})
    out = []
    for d in docs:
        row = _row(d, "job_id")
        row.setdefault("title", None)
        row.setdefault("phase", PHASE_PENDING)
        row.setdefault("detail", None)
        row.setdefault("updated_at", 0)
        out.append(row)
    return out


def cleanup_progress(conn: MongoHandle, job_ids) -> None:
    if not job_ids:
        return
    conn.job_progress.delete_many({"_id": {"$in": [int(j) for j in job_ids]}})


def create_progress_batch(
    conn: MongoHandle, batch_id: str, chat_id: int, job_ids
) -> None:
    # job_ids stays a comma-separated string for exact compatibility with
    # progress_tracker.py, which does: b["job_ids"].split(",")
    ids_str = ",".join(str(int(j)) for j in job_ids)
    conn.progress_batches.update_one(
        {"_id": batch_id},
        {
            "$set": {
                "chat_id": int(chat_id),
                "message_id": None,
                "job_ids": ids_str,
                "created_at": now_ts(),
                "completed_at": None,
            }
        },
        upsert=True,
    )


def set_progress_batch_message(conn: MongoHandle, batch_id: str, message_id: int) -> None:
    conn.progress_batches.update_one(
        {"_id": batch_id}, {"$set": {"message_id": int(message_id)}}
    )


def get_active_progress_batches(conn: MongoHandle) -> list:
    docs = conn.progress_batches.find({"completed_at": None})
    out = []
    for d in docs:
        row = _row(d, "batch_id")
        row.setdefault("message_id", None)
        row.setdefault("job_ids", "")
        row.setdefault("created_at", 0)
        row.setdefault("completed_at", None)
        out.append(row)
    return out


def complete_progress_batch(conn: MongoHandle, batch_id: str) -> None:
    conn.progress_batches.update_one(
        {"_id": batch_id}, {"$set": {"completed_at": now_ts()}}
    )


def delete_progress_batch(conn: MongoHandle, batch_id: str) -> None:
    conn.progress_batches.delete_one({"_id": batch_id})


# ---------------------------------------------------------------------------
# v11 tokens (freepost / /token / /alltoken)
# ---------------------------------------------------------------------------

DEFAULT_FREEPOST = 20


def _today_str() -> str:
    """UTC date string YYYY-MM-DD. Reset boundary is 00:00 UTC."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def get_freepost(conn: MongoHandle) -> int:
    """Current daily token allowance for regular (non-admin) users."""
    v = get_flag(conn, "freepost", str(DEFAULT_FREEPOST))
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return DEFAULT_FREEPOST


def set_freepost(conn: MongoHandle, n: int) -> None:
    set_flag(conn, "freepost", str(max(0, int(n))))


def _touch_user_token_row(
    conn: MongoHandle, user_id: int, username: Optional[str] = None
) -> Dict[str, Any]:
    """Return the user_tokens row for user_id, creating one on first sight.

    Also lazily resets `used_today` to 0 when a new UTC day has begun, and
    refreshes the stored username (users can rename themselves).
    """
    uid = int(user_id)
    today = _today_str()

    # 1) Create on first sight.
    try:
        conn.user_tokens.update_one(
            {"_id": uid},
            {
                "$setOnInsert": {
                    "username": username,
                    "used_today": 0,
                    "last_reset_date": today,
                    "last_search_at": 0,
                }
            },
            upsert=True,
        )
    except DuplicateKeyError:
        pass

    # 2) Reset the counter if the stored date is stale (new UTC day).
    conn.user_tokens.update_one(
        {"_id": uid, "last_reset_date": {"$ne": today}},
        {"$set": {"used_today": 0, "last_reset_date": today}},
    )

    # 3) Keep the username current.
    if username is not None:
        conn.user_tokens.update_one({"_id": uid}, {"$set": {"username": username}})

    doc = conn.user_tokens.find_one({"_id": uid}) or {}
    row = _row(doc, "user_id") or {"user_id": uid}
    row.setdefault("username", None)
    row.setdefault("used_today", 0)
    row.setdefault("last_reset_date", today)
    row.setdefault("last_search_at", 0)
    return row


def get_user_tokens(
    conn: MongoHandle, user_id: int, username: Optional[str] = None
) -> dict:
    """Returns {'used': X, 'remaining': Y, 'daily_cap': Z} for the user."""
    row = _touch_user_token_row(conn, user_id, username)
    cap = get_freepost(conn)
    used = int(row["used_today"])
    return {"used": used, "remaining": max(0, cap - used), "daily_cap": cap}


def consume_tokens(
    conn: MongoHandle, user_id: int, n: int, username: Optional[str] = None
) -> bool:
    """Try to consume `n` tokens atomically. Returns True on success, False if
    the user does not have enough remaining today. Does nothing on False.

    The condition `used_today <= cap - n` lives inside the update filter, so
    the check-and-spend is a single atomic operation. Two simultaneous
    /search confirms therefore cannot both spend the same last token.
    """
    if n <= 0:
        return True
    _touch_user_token_row(conn, user_id, username)
    cap = get_freepost(conn)
    n = int(n)
    if n > cap:
        return False
    doc = conn.user_tokens.find_one_and_update(
        {"_id": int(user_id), "used_today": {"$lte": cap - n}},
        {"$inc": {"used_today": n}, "$set": {"last_search_at": now_ts()}},
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None


def refund_token(conn: MongoHandle, user_id: int, n: int = 1) -> None:
    """Refund `n` tokens (used when a /search-originated job ends 'failed').
    Never lets used_today drop below zero."""
    if n <= 0 or not user_id:
        return
    doc = conn.user_tokens.find_one({"_id": int(user_id)})
    if doc is None:
        return
    used = int(doc.get("used_today") or 0)
    new_used = max(0, used - int(n))
    conn.user_tokens.update_one(
        {"_id": int(user_id)}, {"$set": {"used_today": new_used}}
    )


def set_user_tokens(
    conn: MongoHandle, user_id: int, remaining: int, username: Optional[str] = None
) -> dict:
    """Manually set a user's REMAINING tokens for today (for /settoken).
    Recomputes used_today = daily_cap - remaining, clamped to [0, cap]."""
    _touch_user_token_row(conn, user_id, username)
    cap = get_freepost(conn)
    remaining = max(0, min(int(remaining), cap))
    new_used = cap - remaining
    conn.user_tokens.update_one(
        {"_id": int(user_id)}, {"$set": {"used_today": new_used}}
    )
    return {"used": new_used, "remaining": remaining, "daily_cap": cap}


def reset_all_tokens(conn: MongoHandle) -> int:
    """Force-reset EVERY user's used_today to 0. Returns documents affected."""
    today = _today_str()
    res = conn.user_tokens.update_many(
        {}, {"$set": {"used_today": 0, "last_reset_date": today}}
    )
    return int(res.modified_count or 0)


def list_all_user_tokens(conn: MongoHandle) -> list:
    """All user_tokens rows, refreshed by lazy-reset. Sorted by used_today DESC."""
    today = _today_str()
    conn.user_tokens.update_many(
        {"last_reset_date": {"$ne": today}},
        {"$set": {"used_today": 0, "last_reset_date": today}},
    )
    cap = get_freepost(conn)
    docs = conn.user_tokens.find({}).sort(
        [("used_today", DESCENDING), ("_id", ASCENDING)]
    )
    out: List[dict] = []
    for d in docs:
        used = int(d.get("used_today") or 0)
        out.append(
            {
                "user_id": int(d["_id"]),
                "username": d.get("username") or "",
                "used": used,
                "remaining": max(0, cap - used),
                "daily_cap": cap,
            }
        )
    return out


def resolve_user_id_by_username(conn: MongoHandle, username: str) -> Optional[int]:
    """Look up a user_id from user_tokens by @username (case-insensitive).
    Returns None if that username has never used /search before."""
    if not username:
        return None
    uname = username.lstrip("@").strip()
    if not uname:
        return None
    doc = conn.user_tokens.find_one(
        {"username": {"$regex": f"^{re.escape(uname)}$", "$options": "i"}}
    )
    return int(doc["_id"]) if doc else None


# ---------------------------------------------------------------------------
# Optional async accessor (motor)
# ---------------------------------------------------------------------------
# Every existing call site in this project is synchronous, and PyMongo is
# already fast enough for that. `motor` is installed and exposed here so any
# NEW async code you write can use a non-blocking client without a rewrite:
#
#     adb = await db.get_async_db()
#     doc = await adb["queue"].find_one({"status": "pending"})

_async_client = None
_async_pid: Optional[int] = None


def get_async_db():
    """Return a motor (async) database handle for the same MONGO_URI."""
    global _async_client, _async_pid
    from motor.motor_asyncio import AsyncIOMotorClient

    pid = os.getpid()
    if _async_client is None or _async_pid != pid:
        _async_client = AsyncIOMotorClient(
            _mongo_uri(),
            serverSelectionTimeoutMS=15000,
            appname="mtproto-relay-async",
        )
        _async_pid = pid
    return _async_client[_db_name()]
