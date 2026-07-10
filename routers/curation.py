"""Curation + discovery — extracted from app.py (Phase 2 of the app-split refactor).

Curation re-runs the AI pipeline (manual regenerate/re-enrich + batch RAG enrichment); discovery
runs the multi-source art scouts, ranks/dedupes results into DiscoveryQueueModel, and lets the
Review Queue approve/reject them.
"""

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session

import curator
import scout
from agents import process_artwork
from core.downloads import _download_image_to_library
from core.schemas import ArtworkSchema
from database import SessionLocal, get_db
from models import ArtworkModel, DiscoveryQueueModel
from query_classifier import QueryClassifier
from result_ranker import ResultRanker, clean_title
from scout import create_search_session, get_search_session

logger = logging.getLogger("artwork-display-api")

router = APIRouter()

# Shared instances for smart search
_query_classifier = QueryClassifier()
_result_ranker = ResultRanker()


class RegenerationRequest(BaseModel):
    hint: Optional[str] = None


class DispatchRequest(BaseModel):
    sources: List[str]
    search: Optional[str] = None
    limit: int = 10


class LoadMoreRequest(BaseModel):
    session_id: str


class DiscoveryQueueSchema(BaseModel):
    id: int
    source_url: str
    thumbnail_url: str
    proposed_title: Optional[str] = None
    proposed_artist: Optional[str] = None
    source_api: str
    status: str
    relevance_score: Optional[float] = 0.0
    search_session_id: Optional[str] = None
    model_config = {"from_attributes": True}


async def run_rag_pipeline(artwork_id: int, context_hints: str = None):
    db = SessionLocal()
    try:
        await curator.enrich_artwork(artwork_id, db, context_hints=context_hints)
    finally:
        db.close()

async def run_scouts_bg(query: str = None, sources: List[str] = None,
                       session_id: str = None, limit: int = 10):
    """Background task: classifies query, runs scouts, ranks results, inserts into DB."""
    db = SessionLocal()
    try:
        # Retrieve or create search session
        session = get_search_session(session_id) if session_id else None
        if session:
            intent = session.intent
            offset = session.offset
        else:
            # B1: classify() → sync ai_client.chat (httpx, 90s default) — thread it so a slow provider
            # can't stall this worker's loop.
            intent = await asyncio.to_thread(_query_classifier.classify, query) if query else None
            offset = 0

        logger.info(f"[Scout BG] Starting scouts: query='{query}', sources={sources}, "
                    f"intent={intent.query_type if intent else 'none'}, "
                    f"canonical='{intent.canonical_name if intent else 'n/a'}', "
                    f"offset={offset}, limit={limit}")

        # Run scouts with classified intent
        raw_results = await scout.run_scouts(
            db, query=query, sources=sources,
            intent=intent, offset=offset, limit=limit
        )
        logger.info(f"[Scout BG] Scouts returned {len(raw_results)} raw results")

        # Rank and deduplicate
        ranked_results = _result_ranker.rank_and_deduplicate(raw_results, intent)
        logger.info(f"[Scout BG] After ranking: {len(ranked_results)} results")

        # Insert into DiscoveryQueue, skipping duplicates
        total_new = 0
        for item in ranked_results:
            existing = db.query(DiscoveryQueueModel).filter(
                DiscoveryQueueModel.source_url == item['source_url']
            ).first()
            if not existing:
                item['proposed_title'] = clean_title(item.get('proposed_title'))
                new_entry = DiscoveryQueueModel(
                    **item,
                    search_session_id=session_id
                )
                db.add(new_entry)
                total_new += 1
        db.commit()
        logger.info(f"[Scout BG] DiscoveryQueue updated with {total_new} new items.")
    except Exception as e:
        logger.error(f"[Scout BG] BACKGROUND TASK FAILED: {e}", exc_info=True)
    finally:
        db.close()

async def run_batch_enrich_bg():
    db = SessionLocal()
    try:
        await curator.batch_enrich_all(db)
    finally:
        db.close()


@router.post("/api/curate/regenerate/{artwork_id}", response_model=ArtworkSchema)
async def regenerate_artwork_metadata(artwork_id: int, request: RegenerationRequest, db: Session = Depends(get_db)):
    """Manually triggers the AI pipeline with an optional human-in-the-loop hint."""
    updated_art = await process_artwork(artwork_id, db, user_hint=request.hint)
    if not updated_art:
        raise HTTPException(status_code=500, detail="AI Regeneration failed")
    return updated_art

@router.post("/api/curate/reenrich/{artwork_id}", response_model=ArtworkSchema)
async def reenrich_artwork(artwork_id: int, request: RegenerationRequest, db: Session = Depends(get_db)):
    """Sets artwork status back to pending and triggers AI re-enrichment."""
    art = db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first()
    if not art: raise HTTPException(404)

    art.status = 'pending_review'
    db.commit()

    updated_art = await process_artwork(artwork_id, db, user_hint=request.hint)
    if not updated_art:      # A7: guard like the regenerate sibling — a None fails ArtworkSchema as an ugly 500
        raise HTTPException(status_code=500, detail="AI Re-enrichment failed")
    return updated_art

@router.post("/api/curate/batch-enrich")
async def batch_enrich(background_tasks: BackgroundTasks):
    """Triggers RAG enrichment for all approved artworks."""
    background_tasks.add_task(run_batch_enrich_bg)
    return {"status": "Batch enrichment started in background"}

@router.get("/api/discover/queue", response_model=List[DiscoveryQueueSchema])
async def get_discovery_queue(
    session_id: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Returns the list of pending art discoveries, optionally filtered by session."""
    query = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.status == 'pending')
    if session_id:
        query = query.filter(DiscoveryQueueModel.search_session_id == session_id)
    return query.order_by(DiscoveryQueueModel.relevance_score.desc()).all()

@router.post("/api/discover/dispatch")
async def dispatch_discovery(request: DispatchRequest, background_tasks: BackgroundTasks):
    """Smart multi-source art discovery dispatch with query classification."""
    # Classify the query upfront to create a session with the right intent.
    # B1: thread the sync classify() (→ ai_client.chat, up to 90s) so it can't freeze the worker.
    intent = await asyncio.to_thread(_query_classifier.classify, request.search) if request.search else None
    limit = max(1, min(request.limit, 10))  # Clamp to 1–10

    # Create a search session for Load More support
    session = create_search_session(
        query=request.search or "",
        intent=intent,
        sources=request.sources,
        limit=limit
    )

    background_tasks.add_task(
        run_scouts_bg,
        query=request.search,
        sources=request.sources,
        session_id=session.session_id,
        limit=limit
    )
    return {
        "status": "Art scouts dispatched",
        "sources": request.sources,
        "search": request.search,
        "session_id": session.session_id,
        "intent": {
            "type": intent.query_type if intent else "freetext",
            "canonical": intent.canonical_name if intent else request.search,
        }
    }

@router.post("/api/discover/more")
async def load_more_discoveries(request: LoadMoreRequest, background_tasks: BackgroundTasks):
    """Fetches the next batch of results from an existing search session."""
    session = get_search_session(request.session_id)
    if not session:
        raise HTTPException(404, detail="Search session expired or not found. Please start a new search.")

    # Advance the offset
    session.offset += session.limit

    background_tasks.add_task(
        run_scouts_bg,
        query=session.query,
        sources=session.sources,
        session_id=session.session_id,
        limit=session.limit
    )
    return {
        "status": "Loading more results",
        "session_id": session.session_id,
        "offset": session.offset
    }

@router.post("/api/discover/approve/{item_id}")
async def approve_discovery(item_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Downloads approved discovery and adds to library."""
    item = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.id == item_id).first()
    if not item: raise HTTPException(404)

    # 1. Download full-res image via the shared robust downloader (descriptive UA — Wikimedia/NASA
    #    reject the default httpx UA — plus 429 retry, redirects, and image validation).
    filename = f"scouted_{item_id}_{item.proposed_title.replace(' ', '_')[:50]}"
    _, filename, w, h = await _download_image_to_library(item.source_url, filename=filename)

    # 2. Add to database
    new_art = ArtworkModel(
        filename=filename,
        original_width=w, original_height=h,
        title=item.proposed_title,
        agent_name=item.proposed_artist,
        source_url=item.source_url,
        status='processing'
    )
    db.add(new_art)
    item.status = 'approved'
    db.commit()
    db.refresh(new_art)

    # 3. Enrich with RAG Curator
    background_tasks.add_task(run_rag_pipeline, new_art.id, item.context_hints)

    return {"status": "Art added and fully enriched", "artwork_id": new_art.id}

@router.post("/api/discover/reject/{item_id}")
async def reject_discovery(item_id: int, db: Session = Depends(get_db)):
    """Removes a discovery from the queue."""
    item = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.id == item_id).first()
    if not item: raise HTTPException(404)
    item.status = 'rejected'
    db.commit()
    return {"status": "Rejected"}

@router.delete("/api/discover/history")
async def clear_rejected_history(db: Session = Depends(get_db)):
    """Deletes all rejected items from the discovery queue to free up the cache."""
    db.execute(delete(DiscoveryQueueModel).where(DiscoveryQueueModel.status == 'rejected'))
    db.commit()
    return {"status": "History cleared"}

@router.delete("/api/discover/orphans")
async def clear_orphaned_approvals(db: Session = Depends(get_db)):
    """Deletes discovery queue items that were 'approved' but have no active artwork entry."""
    approved_items = db.query(DiscoveryQueueModel).filter(DiscoveryQueueModel.status == 'approved').all()
    artworks = db.query(ArtworkModel.filename).filter(ArtworkModel.filename.like('scouted_%')).all()

    active_scout_ids = set()
    for (fname,) in artworks:
        parts = fname.split('_')
        if len(parts) >= 2 and parts[1].isdigit():
            active_scout_ids.add(int(parts[1]))

    orphans_deleted = 0
    for item in approved_items:
        if item.id not in active_scout_ids:
            db.delete(item)
            orphans_deleted += 1

    db.commit()
    return {"status": f"Successfully cleared {orphans_deleted} orphaned approvals"}

@router.delete("/api/discover/clear-pending")
async def clear_pending_discoveries(db: Session = Depends(get_db)):
    """Deletes all pending items from the discovery queue. Useful for fresh test runs."""
    result = db.execute(delete(DiscoveryQueueModel).where(DiscoveryQueueModel.status == 'pending'))
    db.commit()
    # Clear any active search sessions
    from scout import _search_sessions
    _search_sessions.clear()
    return {"status": f"Cleared {result.rowcount} pending discoveries"}
