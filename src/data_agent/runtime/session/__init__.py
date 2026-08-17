# session — Couchbase persistence + DI seam (D22/D44/D45).
#
# No re-exports: every caller imports from the submodule that owns the symbol —
# `store.py` (the SessionStore Protocol + its errors), `models.py` (SessionDoc,
# TrailEntry, TurnMessage, ...), `memory_store.py` / `couchbase_store.py` (impls).
