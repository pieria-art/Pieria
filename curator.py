"""
Autonomous RAG Curator for Pieria.
Enriches artwork metadata using Wikipedia context and Gemini.
"""

import asyncio
import json
import logging

import wikipedia
from sqlalchemy.orm import Session

import ai_client
from agents import FOCAL_POINT_INSTRUCTION, apply_focal_point
from config import strip_markdown
from core import grounding
from models import ArtworkModel

logger = logging.getLogger("artwork-display-api.curator")

# Columns grounding may write (record-derived structured fields + prose). `current_repository` and
# `physical_dimensions` have no column today, so they are carried in the result but not stored.
_GROUNDED_COLUMNS = ("agent_name", "creation_date", "date_display", "medium", "cultural_context")


def _user_edited(artwork: ArtworkModel) -> set:
    try:
        return set(json.loads(getattr(artwork, "user_edited_fields", None) or "[]"))
    except (ValueError, TypeError):
        return set()


async def _enrich_grounded(artwork: ArtworkModel, db: Session, context_hints, source_api) -> bool:
    """ADR-148 runtime grounding: identity -> facts -> record-derived fields + validated prose. Returns
    True when the artwork was enriched this way; False means "keep today's behaviour" (nothing to ground
    on, or any failure — grounding never raises)."""
    extra_parts = []
    if artwork.filename:
        from config import LIBRARY_DIR
        img_path = LIBRARY_DIR / artwork.filename
        if img_path.exists():
            try:
                extra_parts.append(ai_client.image_part(str(img_path)))
            except Exception as ie:
                logger.warning(f"[RAG Curator] Image parsing failed: {ie}")
    result = await grounding.ground_work(
        title=artwork.title or "", artist=artwork.agent_name, hints=context_hints,
        source_api=source_api, source_url=artwork.source_url, extra_parts=extra_parts,
        prompt_suffix=FOCAL_POINT_INSTRUCTION + " Put focal_point in the same JSON object.",
    )
    if not result:
        return False
    edited = _user_edited(artwork)     # F8: a field the user edited always wins
    for col in _GROUNDED_COLUMNS:
        if result["fields"].get(col) and col not in edited:
            setattr(artwork, col, result["fields"][col])
    if "description_narrative" not in edited:
        artwork.description_narrative = strip_markdown(result["description_narrative"])
    if result.get("tags") and "tags" not in edited:
        artwork.tags = result["tags"]
    if result.get("focal_point") is not None:
        apply_focal_point(artwork, {"focal_point": result["focal_point"]})
    artwork.status = 'pending_review'
    db.commit()
    ai_client.clear_failure()
    logger.info(f"[RAG Curator] Grounded {artwork.title} ({result['n_facts']} facts, "
                f"{'minimal fallback placard' if result['used_fallback'] else 'validated prose'})")
    return True


async def enrich_artwork(artwork_id: int, db: Session, context_hints: str = None, source_api: str = None):
    """
    Enriches a user-added museum work. First tries grounded enrichment (core/grounding.py: Wikidata
    identity + museum record facts, structured fields from records, validated prose); if that has nothing
    to ground on or fails, falls back to the original Wikipedia-RAG path below. Personal photos never
    enter this pipeline.
    """
    artwork = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not artwork:
        return None
    if artwork.is_personal:
        logger.info(f"[RAG Curator] Skipping personal photo {artwork_id}")
        return artwork

    try:
        if await _enrich_grounded(artwork, db, context_hints, source_api):
            return artwork
    except Exception as e:
        logger.warning(f"[RAG Curator] Grounded enrichment failed, using legacy path: {e}", exc_info=True)
        db.rollback()

    search_query = f"{artwork.title} {artwork.agent_name}"
    logger.info(f"[RAG Curator] Enriching: {search_query}")

    fact_context = ""
    try:
        # Search Wikipedia for the first paragraph summary.
        # B2: wikipedia.summary is a sync `requests` round-trip — thread it (the AI call below already is).
        wiki_page = await asyncio.to_thread(wikipedia.summary, search_query, sentences=3, auto_suggest=True)
        fact_context = wiki_page
        logger.info(f"[RAG Curator] Found Wikipedia context for {artwork.title}")
    except Exception as e:
        logger.warning(f"[RAG Curator] Wikipedia search failed for {search_query}: {e}")
        fact_context = "No additional factual context found."

    try:
        prompt = (
            f"You are a strict museum curator performing RAG (Retrieval-Augmented Generation). "
            f"Current Data: Title: {artwork.title}, Agent: {artwork.agent_name}. "
            f"Factual Context from Wikipedia: \"{fact_context}\" "
        )
        if context_hints:
            prompt += f"Raw JSON Metadata from Museum API: {context_hints} "

        prompt += (
            "Task: Rewrite the museum placard metadata using the Factual Context and Museum API Metadata as the primary source of truth. "
            "If the Wikipedia context contradicts the Museum metadata, prioritize the Museum metadata. "
            "Return ONLY a valid JSON object strictly using these keys: "
            "'title', 'agent_name', 'agent_role' (e.g., 'Painter'), 'creation_date', 'cultural_context' (e.g., 'Dutch'), "
            "'medium' (e.g., 'Oil on canvas'), 'physical_dimensions', 'current_repository', "
            "'date_display' (a formatted string like 'c. 1890', or '19th century'), "
            "'description_narrative' (a 2-sentence blurb), and 'tags' (a flat array of descriptive strings). "
            + FOCAL_POINT_INSTRUCTION
        )

        content_parts = [ai_client.text_part(prompt)]
        if artwork.filename:
            from config import LIBRARY_DIR
            img_path = LIBRARY_DIR / artwork.filename
            if img_path.exists():
                try:
                    content_parts.append(ai_client.image_part(str(img_path)))
                    logger.info(f"[RAG Curator] Attached {artwork.filename} to Vision RAG payload.")
                except Exception as ie:
                    logger.warning(f"[RAG Curator] Image parsing failed: {ie}")

        response_text = await asyncio.to_thread(
            ai_client.chat,
            "vision",
            [{"role": "user", "content": content_parts}],
            json_mode=True,
        )
        metadata = ai_client.parse_json(response_text)

        artwork.title = strip_markdown(metadata.get('title', artwork.title))
        artwork.agent_name = metadata.get('agent_name', artwork.agent_name)
        artwork.agent_role = metadata.get('agent_role', artwork.agent_role)
        artwork.creation_date = metadata.get('creation_date', artwork.creation_date)
        artwork.cultural_context = metadata.get('cultural_context', artwork.cultural_context)
        artwork.medium = metadata.get('medium', artwork.medium)
        artwork.date_display = metadata.get('date_display', getattr(artwork, 'date_display', ''))

        artwork.description_narrative = strip_markdown(metadata.get('description_narrative', getattr(artwork, 'description_narrative', '')))

        tags = metadata.get('tags', [])
        if tags:
            artwork.tags = ", ".join(tags) if isinstance(tags, list) else str(tags)

        apply_focal_point(artwork, metadata)

        artwork.status = 'pending_review'
        db.commit()
        ai_client.clear_failure()
        logger.info(f"[RAG Curator] Successfully enriched {artwork.title}")
        return artwork

    except Exception as e:
        logger.error(f"[RAG Curator] Gemini enrichment failed: {e}", exc_info=True)
        db.rollback()
        ai_client.record_failure(e)   # surfaced on the Review Queue banner; see ai_client.record_failure
        artwork.status = 'pending_review'
        db.add(artwork)
        db.commit()
        return None

async def batch_enrich_all(db: Session):
    """
    Runs enrichment on all approved artworks with rate-limiting.
    """
    artworks = db.query(ArtworkModel).filter(ArtworkModel.status == 'approved').all()
    logger.info(f"[RAG Curator] Starting batch enrichment for {len(artworks)} items.")

    for art in artworks:
        await enrich_artwork(art.id, db)
        await asyncio.sleep(2) # Rate-limiting delay
