"""Guard (ADR-148, 1.1 part A): no `async def` in these routers runs sync SQLAlchemy on the event loop.

DB-only routes are plain `def` (FastAPI runs them in the threadpool). Routes that genuinely await
(httpx / websockets / streaming / asyncio) stay `async def` and do their DB work in a nested sync helper
run via `run_in_threadpool` on its OWN short-lived `SessionLocal()` -- never a request-scoped
`Depends(get_db)` session (it would be used from another thread and held across awaits).

The scan fails on an `async def` whose own body (nested sync `def`s / lambdas are the wrapped helpers and
are skipped) uses `db.query/execute/commit/...`, opens `SessionLocal()` directly, or takes a
`Depends(get_db)` parameter -- unless it is on the documented allowlist below.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "routers"

ROUTERS = ["library", "settings", "publisher", "curation"]

DB_METHODS = {"query", "execute", "commit", "add", "add_all", "delete", "refresh", "rollback", "flush", "scalar"}

# (file, function) -> why an async def may touch a session directly.
ALLOWLIST = {
    ("library.py", "run_ai_pipeline"): (
        "BackgroundTasks job, not a route: opens its own SessionLocal for the async agents.process_artwork "
        "(awaits LLM calls between DB steps); already off the request session (rule 6)."),
    ("curation.py", "run_rag_pipeline"): (
        "BackgroundTasks job, not a route: own SessionLocal handed to async curator.enrich_artwork."),
    ("curation.py", "run_scouts_bg"): (
        "BackgroundTasks job, not a route: own SessionLocal handed to async scout.run_scouts; the small "
        "DiscoveryQueue insert loop runs after the awaits (pre-existing, behaviour deliberately unchanged)."),
    ("curation.py", "run_batch_enrich_bg"): (
        "BackgroundTasks job, not a route: own SessionLocal handed to async curator.batch_enrich_all."),
    ("curation.py", "regenerate_artwork_metadata"): (
        "route whose whole job is awaiting agents.process_artwork(artwork_id, db), an async pipeline that "
        "interleaves LLM awaits with DB use on the session it is handed. Threading it means reworking "
        "agents.process_artwork -- out of scope for part A."),
    ("curation.py", "reenrich_artwork"): (
        "same as regenerate_artwork_metadata: status flip + await agents.process_artwork on one session."),
}


def _own_nodes(fn):
    """Nodes in fn's body, NOT descending into nested sync defs / lambdas (those are the wrapped helpers).
    Nested async defs are skipped here too; the caller scans every async def in its own right."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _violations(path: Path):
    tree = ast.parse(path.read_text())
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if (path.name, fn.name) in ALLOWLIST:
            continue
        for d in fn.args.defaults + [x for x in fn.args.kw_defaults if x is not None]:
            if (isinstance(d, ast.Call) and getattr(d.func, "id", None) == "Depends"
                    and d.args and getattr(d.args[0], "id", None) == "get_db"):
                out.append(f"{path.name}:{fn.lineno} async {fn.name}: Depends(get_db) parameter")
        for n in _own_nodes(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if (isinstance(f, ast.Attribute) and f.attr in DB_METHODS and isinstance(f.value, ast.Name)
                        and f.value.id in ("db", "session")):
                    out.append(f"{path.name}:{n.lineno} async {fn.name}: db.{f.attr}(...) on the event loop")
                if getattr(f, "id", None) == "SessionLocal":
                    out.append(f"{path.name}:{n.lineno} async {fn.name}: SessionLocal() opened on the event loop")
    return out


def test_no_sync_db_in_async_routes():
    bad = []
    for name in ROUTERS:
        bad += _violations(ROOT / f"{name}.py")
    assert not bad, ("sync DB work inside async def (wrap it in run_in_threadpool or make the route `def`):\n"
                     + "\n".join(bad))


def test_allowlist_entries_still_exist():
    """A stale allowlist silently exempts nothing -- keep it honest."""
    for (fname, func) in ALLOWLIST:
        tree = ast.parse((ROOT / fname).read_text())
        assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == func for n in ast.walk(tree)), (fname, func)


def test_guard_catches_a_violation(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("async def r(db=Depends(get_db)):\n    db.query(1)\n")
    assert len(_violations(p)) == 2
