"""
AI Agents for Artwork Analysis using Gemini API.
Phase 3: Automated Metadata Generation with Image Optimization.
"""

import asyncio
import logging
from pathlib import Path

from PIL import Image
from sqlalchemy.orm import Session

import ai_client
from config import strip_markdown
from models import ArtworkModel

# Increase Pillow limit for high-res artwork
Image.MAX_IMAGE_PIXELS = 200000000

logger = logging.getLogger("artwork-display-api.agents")

# Shared focal-point derivation, used by both vision passes (this module's upload analysis and the
# RAG curator) so the instruction + parsing can't drift between them.
FOCAL_POINT_INSTRUCTION = (
    "Also include 'focal_point': the [x, y] location of the composition's main visual subject — the "
    "point a viewer's eye is drawn to that must stay in frame when the image is cropped or panned "
    "(e.g. a portrait's face, the principal figure or object). Use normalized coordinates where "
    "[0, 0] is the top-left corner and [1, 1] is the bottom-right (a face in the upper middle is "
    "about [0.5, 0.3]); use [0.5, 0.5] only when there is no single clear subject."
)


def apply_focal_point(artwork: ArtworkModel, metadata: dict) -> None:
    """Store an optional 'focal_point': [x, y] (normalized 0..1) from a vision response as the
    artwork's framing anchor. Tolerant of missing/malformed values (keeps the prior focal point)."""
    fp = metadata.get("focal_point")
    if not (isinstance(fp, (list, tuple)) and len(fp) == 2):
        return
    try:
        x = min(1.0, max(0.0, float(fp[0])))
        y = min(1.0, max(0.0, float(fp[1])))
    except (TypeError, ValueError):
        return  # parse both before assigning — never leave a half-applied focal point
    artwork.focal_x = x
    artwork.focal_y = y


async def process_artwork(artwork_id: int, db: Session, user_hint: str = None):
    """
    Analyzes artwork using Gemini 2.5 Flash.
    Optimizes image size before sending to prevent timeouts.
    """
    logger.info(f"[AI Agent] Starting analysis for artwork ID: {artwork_id} (Hint: {user_hint or 'None'})")

    artwork = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not artwork: return

    from config import LIBRARY_DIR
    image_path = LIBRARY_DIR / artwork.filename

    if not image_path.exists(): return

    # Clean filename for context
    clean_filename = Path(artwork.filename).stem.replace("_", " ").replace("-", " ")

    try:
        # Optimization: Resize for AI processing (max 2048px) handled by ai_client.image_part.
        image_data = ai_client.image_part(str(image_path))

        system_instruction = (
            "You are a strict, factual museum curator. I am providing an image. "
            f"The original filename was \"{clean_filename}\". Use this filename as a hint for the title or artist ONLY if it contains readable words; "
            "ignore it if it looks like random letters/numbers. "
        )

        if user_hint:
            system_instruction += f"If a User Hint is provided: \"{user_hint}\", treat it as absolute fact and build your description around it. "

        system_instruction += (
            "If you cannot confidently identify the creator from the visual data or hints, explicitly state \"Unknown Artist\" rather than guessing. "
            "Return ONLY a valid JSON object strictly using these keys: "
            "'title', 'agent_name', 'agent_role' (e.g., 'Painter', 'Attributed to'), 'creation_date', 'cultural_context' (e.g., 'Dutch', 'Post-Impressionist'), "
            "'medium' (e.g., 'Oil on canvas'), 'physical_dimensions', 'current_repository' (museum location if known, else 'Unknown'), "
            "'date_display' (a formatted string like 'c. 1890', or '19th century'), "
            "'description_narrative' (a 2-sentence museum-style blurb), "
            "and 'tags' (a flat array of 5-10 descriptive strings covering mood, subject, style, "
            "and season if applicable). "
            + FOCAL_POINT_INSTRUCTION
        )

        response_text = await asyncio.to_thread(
            ai_client.chat,
            "vision",
            [{"role": "user", "content": [ai_client.text_part(system_instruction), image_data]}],
            json_mode=True,
        )

        metadata = ai_client.parse_json(response_text)
        logger.info(f"[AI Agent] Metadata generated for {artwork.filename}")

        artwork.title = strip_markdown(metadata.get('title', 'Untitled'))
        artwork.agent_name = metadata.get('agent_name', 'Unknown Artist')
        artwork.agent_role = metadata.get('agent_role', 'Artist')
        artwork.creation_date = metadata.get('creation_date', 'Unknown')
        artwork.cultural_context = metadata.get('cultural_context', '')
        artwork.medium = metadata.get('medium', '')
        artwork.date_display = metadata.get('date_display', '')

        artwork.description_narrative = strip_markdown(metadata.get('description_narrative', ''))

        tags = metadata.get('tags', [])
        artwork.tags = ", ".join(tags) if isinstance(tags, list) else str(tags)

        apply_focal_point(artwork, metadata)

        db.commit()
        return artwork # Return updated object

    except Exception as e:
        logger.error(f"[AI Agent] AI processing failed: {e}", exc_info=True)
        db.rollback()
        return None
