"""Per-field user-edit tracking on artworks (ADR-148 F8: a field the user edited always wins a pack
refresh). `artworks.user_edited_fields` is a JSON array of ArtworkModel column names. Only USER edit
routes call these; pack installs, seeds and AI enrichment never do."""
import json


def edited_fields(art) -> set:
    try:
        v = json.loads(getattr(art, "user_edited_fields", None) or "[]")
    except (TypeError, ValueError):
        return set()
    return {f for f in v if isinstance(f, str)} if isinstance(v, list) else set()


def mark_edited(art, fields) -> None:
    """Add `fields` (column names) to the artwork's edited set. Caller commits."""
    cur = edited_fields(art)
    new = cur | set(fields)
    if new != cur:
        art.user_edited_fields = json.dumps(sorted(new))


def assign_tracked(art, values: dict) -> None:
    """Set each column in `values`, marking only the ones whose value actually CHANGED (an edit form
    that re-saves untouched fields must not freeze them against pack corrections)."""
    changed = []
    for k, v in values.items():
        cur = getattr(art, k)
        if cur != v and not (cur in (None, "") and v in (None, "")):   # a blank form field == NULL
            setattr(art, k, v)
            changed.append(k)
    mark_edited(art, changed)
