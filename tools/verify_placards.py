"""Independent verification of every re-grounded placard (ADR-140).

The re-grounding pipeline (tools/reground_placards.py) retrieves facts and writes narratives with one
model family (Gemini). This tool is a DIFFERENT model family's check: it sends each work's final header
+ narrative + the facts bundle that produced it to the in-lab local model (Qwen 27B, behind the lab's
LiteLLM gateway — see ~/ai-workspace/infrastructure/mcp/local-llm) and asks a strict, literal judge
whether the facts actually describe this object, whether header fields are contradicted, and whether
narrative claims are unsupported or contradicted. Text only — no images.

Read-only against reground_placards.py (imported nothing from it; the tiny normaliser below is copied,
not imported, to keep this module's own dependency footprint at stdlib only). The chat transport is
injected so tests never touch the network; the CLI wires up the real one lazily
(local_llm_mcp.server._chat) so importing this module never requires the MCP package or network access.

    python -m tools.verify_placards --limit 20
    python -m tools.verify_placards --summary

See tools/reground_placards.py's module docstring for the pipeline this checks, and
~/pieria-img/reground/WRITER_INSTRUCTIONS.md for what the narratives were instructed to do.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable

REGROUND_DIR = Path.home() / "pieria-img" / "reground"
DEFAULT_CATALOG_DIR = REGROUND_DIR / "catalog"
DEFAULT_PACKETS_DIR = REGROUND_DIR / "packets"
DEFAULT_OUTPUT_DIR = Path.home() / "pieria-img" / "verify"

HEADER_FIELDS = ["title", "agent_name", "date_display", "medium", "current_repository", "physical_dimensions"]

SYSTEM_PROMPT = (
    "You verify museum placards for a public-domain art catalog. You are strict and literal. "
    "Answer ONLY with a JSON object, no prose."
)

# tuple[str|None, str|None] == (content, error), matching local_llm_mcp.server._chat's return shape.
ChatFn = Callable[..., Awaitable[tuple[str | None, str | None]]]


# ----------------------------------------------------------------------------------------- normalise
def _norm(s: str) -> str:
    """Copied from tools.audit_placards._norm (not imported — see module docstring)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------------------- catalog I/O
def load_catalog_items(catalog_dir: Path, collection_filter: str | None = None):
    """Yield (collection, item_dict) for every work in every `<collection>.json` in catalog_dir.

    Catalog files may hold either a bare list of items or {"items": [...]} — same shape as
    static/catalog.
    """
    for f in sorted(catalog_dir.glob("*.json")):
        collection = f.stem
        if collection_filter and collection != collection_filter:
            continue
        data = json.loads(f.read_text())
        items = data.get("items") if isinstance(data, dict) else data
        if items is None:
            items = data if isinstance(data, list) else []
        for item in items:
            yield collection, item


# ------------------------------------------------------------------------------------- packet lookup
class PacketIndex:
    """Maps a catalog (collection, title) to its facts packet.

    Never matched positionally: the import may have dropped items, shifting `item_key`'s idx — so this
    is built by scanning packets/<collection>/*.json and matching on (collection, normalised title,
    normalised agent_name), falling back to title-only when that's unique within the collection.
    Ambiguous or absent matches are reported by the caller, never guessed.
    """

    def __init__(self, packets_dir: Path):
        self.by_title_agent: dict[tuple[str, str, str], Path] = {}
        self.by_title: dict[tuple[str, str], list[Path]] = {}
        self.load_errors: list[str] = []
        if not packets_dir.exists():
            return
        for coll_dir in sorted(p for p in packets_dir.iterdir() if p.is_dir()):
            collection = coll_dir.name
            for pf in sorted(coll_dir.glob("*.json")):
                try:
                    data = json.loads(pf.read_text())
                except Exception as e:
                    self.load_errors.append(f"{pf}: {e}")
                    continue
                cat = data.get("catalog") or {}
                title = cat.get("title") or data.get("title") or ""
                agent = cat.get("agent_name") or ""
                nt = _norm(title)
                if not nt:
                    continue
                na = _norm(agent)
                self.by_title_agent[(collection, nt, na)] = pf
                self.by_title.setdefault((collection, nt), []).append(pf)

    def find(self, collection: str, title: str, agent_name: str) -> tuple[Path | None, str]:
        """Returns (packet_path_or_None, reason). reason is "title+agent" / "title-only" /
        "ambiguous" / "unmatched" for callers that need to report why a match failed."""
        nt = _norm(title)
        na = _norm(agent_name)
        p = self.by_title_agent.get((collection, nt, na))
        if p is not None:
            return p, "title+agent"
        candidates = self.by_title.get((collection, nt), [])
        if len(candidates) == 1:
            return candidates[0], "title-only"
        if len(candidates) > 1:
            return None, "ambiguous"
        return None, "unmatched"


def load_packet(path: Path) -> dict:
    return json.loads(path.read_text())


# ------------------------------------------------------------------------------------------- prompt
def _fmt_fact(fact: dict) -> str:
    value = fact.get("value")
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return f"{fact.get('key')}: {value}"


def build_messages(collection: str, header: dict, narrative: str, facts: list[dict]) -> list[dict]:
    work_lines = [f"{field}: {header.get(field) or ''}" for field in HEADER_FIELDS]
    facts_lines = [_fmt_fact(f) for f in facts] if facts else ["(no facts retrieved)"]
    user = (
        "WORK (catalog header):\n"
        + "\n".join(work_lines)
        + f"\n\nPLACARD NARRATIVE: \"{narrative or ''}\"\n\n"
        + "FACTS (retrieved; each may come from a WRONG match):\n"
        + "\n".join(facts_lines)
        + f"\n\nCatalog collection: {collection}\n\n"
        + "Respond with a JSON object matching this schema:\n"
        + '{"identity": "same"|"different_object"|"different_version"|"unclear", '
        + '"identity_reason": "...", '
        + '"header_issues": [{"field": "...", "problem": "...", "evidence": "..."}], '
        + '"narrative_issues": [{"claim": "...", "problem": "unsupported"|"contradicted", "evidence": "..."}], '
        + '"severity": "none"|"minor"|"major"}\n\n'
        + "Rules: a fact that describes a different physical object (a painted study, the literary "
        + "source, another version, a copy) must not be used for this work's medium, repository, "
        + "dimensions or date. Visual description of the image is allowed and is not \"unsupported\". "
        + "An empty or missing header field is NOT an issue — only report a field whose stated value is wrong. "
        + "Severity major = a wrong identity, a wrong header field a visitor would read as fact "
        + "(medium, date, artist, repository), or a false factual claim in the narrative. minor = "
        + "imprecise but not false."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_verdict(content: str) -> tuple[dict | None, str | None]:
    try:
        return json.loads(strip_fences(content)), None
    except Exception as e:
        return None, str(e)


# --------------------------------------------------------------------------------------------- judge
async def judge_one(
    chat_fn: ChatFn, collection: str, header: dict, narrative: str, facts: list[dict]
) -> tuple[dict | None, str | None, str | None]:
    """Returns (verdict_or_None, raw_content_on_final_failure, error_or_None)."""
    messages = build_messages(collection, header, narrative, facts)
    extra = {"chat_template_kwargs": {"enable_thinking": True}}

    content, err = await chat_fn(messages, max_tokens=4000, temperature=0.1, extra=extra)
    if content is None:
        return None, None, err

    verdict, perr = parse_verdict(content)
    if verdict is not None:
        return verdict, None, None

    # one retry on invalid JSON
    content2, err2 = await chat_fn(messages, max_tokens=4000, temperature=0.1, extra=extra)
    if content2 is None:
        return None, content, err2 or "retry produced no content"

    verdict2, perr2 = parse_verdict(content2)
    if verdict2 is not None:
        return verdict2, None, None
    return None, content2, f"invalid JSON after retry: {perr2}"


# ------------------------------------------------------------------------------------------- runner
def _output_path(output_dir: Path, collection: str, key: str) -> Path:
    return output_dir / collection / f"{key}.json"


def _load_sample_filter(sample_path: Path) -> set[tuple[str, str]]:
    data = json.loads(sample_path.read_text())
    return {(row["collection"], _norm(row["title"])) for row in data}


async def run(
    catalog_dir: Path,
    packets_dir: Path,
    output_dir: Path,
    chat_fn: ChatFn,
    limit: int | None = None,
    collection: str | None = None,
    sample: Path | None = None,
    concurrency: int = 2,
) -> dict[str, Any]:
    """Verify every (matched, not-yet-done) work; returns a run summary dict.

    Resumable: a work whose output file already exists is skipped without calling the model.
    """
    index = PacketIndex(packets_dir)
    sample_filter = _load_sample_filter(sample) if sample else None

    todo: list[tuple[str, dict, Path]] = []
    unmatched: list[dict] = []
    skipped_done = 0

    for coll, item in load_catalog_items(catalog_dir, collection):
        title = item.get("title") or ""
        if sample_filter is not None and (coll, _norm(title)) not in sample_filter:
            continue
        packet_path, reason = index.find(coll, title, item.get("agent_name") or "")
        if packet_path is None:
            unmatched.append({"collection": coll, "title": title, "reason": reason})
            continue
        packet = load_packet(packet_path)
        key = packet.get("key") or packet_path.stem
        out_path = _output_path(output_dir, coll, key)
        if out_path.exists():
            skipped_done += 1
            continue
        todo.append((coll, item, packet))
        if limit is not None and len(todo) >= limit:
            break

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict] = []

    async def _one(coll: str, item: dict, packet: dict) -> dict:
        header = {f: item.get(f) for f in HEADER_FIELDS}
        header["collection"] = coll
        narrative = item.get("description_narrative") or ""
        facts = packet.get("facts") or []
        key = packet.get("key")
        async with sem:
            start = time.monotonic()
            verdict, raw, err = await judge_one(chat_fn, coll, header, narrative, facts)
            elapsed_s = round(time.monotonic() - start, 3)
        record = {
            "collection": coll,
            "key": key,
            "title": item.get("title"),
            "header": header,
            "verdict": verdict,
            "raw_on_parse_failure": raw,
            "error": err,
            "elapsed_s": elapsed_s,
        }
        out_path = _output_path(output_dir, coll, key)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, indent=2))
        return record

    if todo:
        results = await asyncio.gather(*(_one(c, i, p) for c, i, p in todo))

    return {
        "attempted": len(todo),
        "skipped_already_done": skipped_done,
        "unmatched": unmatched,
        "results": results,
    }


# ------------------------------------------------------------------------------------------ summary
def summarize(output_dir: Path) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    by_identity: dict[str, int] = {}
    flagged: list[dict] = []
    n = 0
    for f in sorted(output_dir.glob("*/*.json")):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        n += 1
        verdict = rec.get("verdict") or {}
        severity = verdict.get("severity", "parse_failure" if rec.get("verdict") is None else "none")
        identity = verdict.get("identity", "parse_failure" if rec.get("verdict") is None else "same")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_identity[identity] = by_identity.get(identity, 0) + 1
        if severity in ("major", "minor") or identity != "same":
            flagged.append(rec)

    order = {"major": 0, "minor": 1, "none": 2, "parse_failure": 3}
    flagged.sort(key=lambda r: order.get((r.get("verdict") or {}).get("severity", "parse_failure"), 3))

    flagged_path = output_dir / "flagged.json"
    flagged_path.write_text(json.dumps(flagged, indent=2))

    return {
        "total": n,
        "by_severity": by_severity,
        "by_identity": by_identity,
        "flagged_count": len(flagged),
        "flagged_path": str(flagged_path),
    }


# ---------------------------------------------------------------------------------------------- CLI
def _load_real_chat_fn() -> ChatFn:
    from local_llm_mcp.server import _chat  # lazy: only the launcher's env can import this

    async def chat_fn(messages, *, max_tokens, temperature, extra=None):
        return await _chat("verify_placard", messages, max_tokens, temperature, extra=extra)

    return chat_fn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    ap.add_argument("--packets-dir", type=Path, default=DEFAULT_PACKETS_DIR)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--collection", type=str, default=None)
    ap.add_argument("--sample", type=Path, default=None)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--summary", action="store_true", help="aggregate existing output and write flagged.json")
    args = ap.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.summary:
        summary = summarize(args.output_dir)
        print(json.dumps(summary, indent=2))
        return 0

    chat_fn = _load_real_chat_fn()
    result = asyncio.run(
        run(
            args.catalog_dir,
            args.packets_dir,
            args.output_dir,
            chat_fn,
            limit=args.limit,
            collection=args.collection,
            sample=args.sample,
            concurrency=args.concurrency,
        )
    )
    print(
        json.dumps(
            {
                "attempted": result["attempted"],
                "skipped_already_done": result["skipped_already_done"],
                "unmatched_count": len(result["unmatched"]),
            },
            indent=2,
        )
    )
    if result["unmatched"]:
        print("unmatched:")
        for u in result["unmatched"]:
            print(f"  [{u['reason']}] {u['collection']}: {u['title']}")
    for r in result["results"]:
        v = r.get("verdict") or {}
        print(f"  {r['collection']}/{r['key']}: identity={v.get('identity')} severity={v.get('severity')} ({r['elapsed_s']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
