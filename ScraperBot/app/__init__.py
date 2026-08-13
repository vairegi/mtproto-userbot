"""ScraperBot (BOT 1) — isolated cache warmer for the DoujinshiUniverse
Mongo + Turso layer. Reads/writes ONLY the same cache keys BOT 0 already
uses. Never touches BOT 0's queue, users, admins, or galleries state."""
