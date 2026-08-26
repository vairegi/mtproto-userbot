

# v12.51: re-export turso_schema so `from app.services import turso_schema` works
# even when the module is added later; falls back silently if missing.
try:  # noqa: SIM105
    from . import turso_schema  # type: ignore  # noqa: F401
except ImportError:
    pass
