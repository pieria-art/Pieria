"""
Modular Semantic Art Scout for Pieria.
Discovers new high-resolution public-domain art.

Smart Search: Uses QueryClassifier to dispatch API-specific optimized queries.
"""

import asyncio
import difflib
import json
import logging
import random
import re
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from models import SettingsModel
from query_classifier import SearchIntent

logger = logging.getLogger("artwork-display-api.scout")

# ---------------------------------------------------------------------------
# Shared source helpers (canonical home).
# The offline catalog builder (tools/sources.py) imports these so live Scouts
# and the builder apply identical PD / resolution logic and never drift.
# ---------------------------------------------------------------------------

# A display image must be at least this many pixels on its long edge to look
# good on big (up-to-4K) displays. Used to gate Wikimedia originals.
MIN_DISPLAY_EDGE = 2000

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s) -> str:
    return _TAG_RE.sub("", s or "").strip()


def _ratio(a, b) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _wikimedia_filepath(filename: str, width: int) -> str:
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename)}?width={width}"


def _wm_match(fname: str, title: str, artist: str = "") -> float:
    """Score a Commons filename against a wanted work. Commons filenames embed artist/date
    (e.g. 'Mucha-Job-1896.jpg'), so a plain ratio underrates short titles like 'Job' — instead
    reward title-token containment, then nudge for the artist surname."""
    f = re.sub(r"[_\-]+", " ", fname.rsplit(".", 1)[0]).lower()
    toks = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2]
    if toks and all(t in f for t in toks):
        score = 0.9
    else:
        present = (sum(1 for t in toks if t in f) / len(toks)) if toks else 0.0
        score = max(present, _ratio(f, title))
    if artist:
        surname = artist.lower().split()[-1]
        if len(surname) > 2 and surname in f:
            score = min(1.0, score + 0.15)
    return score


def _wm_is_pd(extmeta: dict) -> bool:
    parts = []
    for k in ("LicenseShortName", "License", "UsageTerms"):
        v = (extmeta.get(k) or {}).get("value")
        if v:
            parts.append(str(v).lower())
    blob = " ".join(parts)
    if "public domain" in blob or "cc0" in blob or "pd-" in blob:
        return True
    if (extmeta.get("Copyrighted") or {}).get("value", "").strip().lower() in ("false", "no"):
        return True
    return False


# Polite Wikimedia rate limiting: one request at a time, spaced out, descriptive UA.
_wm_lock = asyncio.Lock()
_wm_last = [0.0]
WM_MIN_INTERVAL = 1.2


async def _wm_throttle():
    async with _wm_lock:
        wait = WM_MIN_INTERVAL - (time.monotonic() - _wm_last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _wm_last[0] = time.monotonic()


async def _nasa_best_asset(client, nasa_id: str):
    """Resolve the real full-resolution image from a NASA asset manifest (prefer ~orig/~large jpg).
    The search 'links' only give a thumbnail, and naive ~orig.jpg guessing often 404s."""
    try:
        r = await client.get(f"https://images-assets.nasa.gov/image/{nasa_id}/collection.json", timeout=20.0)
        if r.status_code != 200:
            return None
        urls = [u for u in r.json() if isinstance(u, str) and u.lower().endswith((".jpg", ".jpeg", ".png"))]
    except Exception:
        return None
    if not urls:
        return None
    for suffix in ("~orig.jpg", "~orig.jpeg", "~orig.png", "~large.jpg", "~medium.jpg"):
        for u in urls:
            if u.lower().endswith(suffix):
                return u
    return urls[-1]

class MuseumScout(ABC):
    @abstractmethod
    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        """Returns a list of art dictionaries with source_url, thumbnail_url, etc."""
        pass


class ChicagoArtScout(MuseumScout):
    """
    Scout for the Art Institute of Chicago.
    Uses Elasticsearch DSL for targeted field queries.
    """
    API_URL = "https://api.artic.edu/api/v1/artworks/search"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = query or "painting"
        logger.info(f"[Scout] ChicagoArtScout searching for: {q} (intent: {intent.query_type if intent else 'none'}, offset: {offset})")
        found = []
        headers = {"User-Agent": "Pieria/1.0"}

        try:
            async with httpx.AsyncClient(headers=headers) as client:
                # Build query params based on intent type
                params = {
                    "fields": "id,title,artist_title,image_id,date_display,medium_display,"
                              "classification_titles,style_titles,is_boosted,thumbnail",
                    "limit": limit,
                    "page": (offset // limit) + 1,
                }

                if intent and intent.query_type == "artist":
                    # Use canonical name for better artist matching via q param
                    # Chicago's q param naturally boosts artist_title field matches
                    params["q"] = intent.canonical_name
                    params["query[term][is_public_domain]"] = "true"
                elif intent and intent.query_type == "genre":
                    # Genre search — q param matches against style_titles, classification_titles
                    params["q"] = intent.canonical_name
                    params["query[term][is_public_domain]"] = "true"
                else:
                    # Freetext / subject: use generic q param
                    params["q"] = q
                    params["query[term][is_public_domain]"] = "true"

                response = await client.get(self.API_URL, params=params, timeout=15.0)
                if response.status_code != 200:
                    logger.error(f"[Scout] Chicago API returned {response.status_code}")
                    return []

                data = response.json()
                artworks = data.get('data', [])
                iiif_base = data.get('config', {}).get('iiif_url', 'https://www.artic.edu/iiif/2')

                for art in artworks:
                    image_id = art.get('image_id')
                    if not image_id:
                        continue

                    full_url = f"{iiif_base}/{image_id}/full/max/0/default.jpg"
                    thumb_url = f"{iiif_base}/{image_id}/full/400,/0/default.jpg"

                    found.append({
                        "source_url": full_url,
                        "thumbnail_url": thumb_url,
                        "proposed_title": art.get('title') or 'Unknown',
                        "proposed_artist": art.get('artist_title') or 'Unknown Artist',
                        "source_api": "Art Institute of Chicago",
                        "context_hints": json.dumps(art)
                    })
        except Exception:
            logger.error(f"[Scout] ChicagoArtScout failed: {traceback.format_exc()}")
        return found


class MetMuseumScout(MuseumScout):
    """
    Scout for the Metropolitan Museum of Art.
    Uses artistOrCulture and isHighlight flags for targeted queries.
    """
    SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1/search"
    OBJECT_URL = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = query or "painting"
        logger.info(f"[Scout] MetMuseumScout searching for: {q} (intent: {intent.query_type if intent else 'none'}, offset: {offset})")
        found = []
        headers = {"User-Agent": "Pieria/1.0"}

        try:
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                params = {"q": intent.canonical_name if intent and intent.query_type == "artist" else q,
                           "hasImages": "true"}

                if intent and intent.query_type == "artist":
                    params["artistOrCulture"] = "true"
                elif intent and intent.query_type == "genre":
                    params["isHighlight"] = "true"

                response = await client.get(self.SEARCH_URL, params=params)
                if response.status_code != 200:
                    logger.error(f"[Scout] Met search returned {response.status_code}")
                    return []

                data = response.json()
                object_ids = data.get('objectIDs') or []  # Handle null explicitly
                logger.info(f"[Scout] Met search returned {len(object_ids)} object IDs (total: {data.get('total', 0)})")

                # Fallback: if artistOrCulture returned nothing, retry without it
                if not object_ids and intent and intent.query_type == "artist":
                    logger.info("[Scout] Met: artistOrCulture returned 0, retrying broad search...")
                    fallback_params = {"q": intent.canonical_name, "hasImages": "true"}
                    response = await client.get(self.SEARCH_URL, params=fallback_params)
                    if response.status_code == 200:
                        data = response.json()
                        object_ids = data.get('objectIDs') or []
                        logger.info(f"[Scout] Met fallback returned {len(object_ids)} object IDs")

                # Fallback: if isHighlight returned nothing for genre, retry without it
                if not object_ids:
                    if intent and intent.query_type == "genre" and "isHighlight" in params:
                        logger.info("[Scout] Met: No highlights found, retrying without isHighlight...")
                        del params["isHighlight"]
                        response = await client.get(self.SEARCH_URL, params=params)
                        if response.status_code == 200:
                            data = response.json()
                            object_ids = data.get('objectIDs') or []
                    if not object_ids:
                        logger.info("[Scout] Met: No results after fallbacks")
                        return []

                # Paginate: take a slice based on offset and limit
                selected_ids = object_ids[offset:offset + limit]
                if not selected_ids:
                    return []

                # Fetch object details sequentially with small delay
                # Met API rate-limits aggressive concurrent requests
                logger.info(f"[Scout] Met: Fetching details for {len(selected_ids)} objects: {selected_ids}")
                for obj_id in selected_ids:
                    try:
                        resp = await client.get(self.OBJECT_URL.format(id=obj_id))
                        if resp.status_code == 200:
                            obj_data = resp.json()
                            img_url = obj_data.get('primaryImage')
                            if not img_url:
                                logger.debug(f"[Scout] Met object {obj_id}: no primaryImage, skipping")
                                continue

                            found.append({
                                "source_url": img_url,
                                "thumbnail_url": obj_data.get('primaryImageSmall') or img_url,
                                "proposed_title": obj_data.get('title') or 'Unknown',
                                "proposed_artist": obj_data.get('artistDisplayName') or 'Unknown Artist',
                                "source_api": "The Metropolitan Museum of Art",
                                "context_hints": json.dumps(obj_data)
                            })
                        else:
                            logger.warning(f"[Scout] Met object {obj_id}: HTTP {resp.status_code}")
                    except Exception as e:
                        logger.warning(f"[Scout] Met object {obj_id} fetch failed: {e}")
                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.2)

        except Exception:
            logger.error(f"[Scout] MetMuseumScout failed: {traceback.format_exc()}")
        logger.info(f"[Scout] Met: returning {len(found)} artworks")
        return found


class ClevelandArtScout(MuseumScout):
    """
    Scout for the Cleveland Museum of Art.
    Uses dedicated 'artists' param and 'highlight' flag.
    """
    API_URL = "https://openaccess-api.clevelandart.org/api/artworks/"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = query or "painting"
        logger.info(f"[Scout] ClevelandArtScout searching for: {q} (intent: {intent.query_type if intent else 'none'}, offset: {offset})")
        found = []
        headers = {"User-Agent": "Pieria/1.0"}

        try:
            async with httpx.AsyncClient(headers=headers) as client:
                params = {"has_image": "1", "cc0": "1", "limit": limit, "skip": offset}

                if intent and intent.query_type == "artist":
                    # Use dedicated artists param for precise artist search
                    params["artists"] = intent.canonical_name
                elif intent and intent.query_type == "genre":
                    params["q"] = intent.canonical_name
                    params["type"] = "Painting"
                    params["highlight"] = "1"
                else:
                    params["q"] = q

                response = await client.get(self.API_URL, params=params, timeout=15.0)
                if response.status_code != 200:
                    return []

                data = response.json()
                artworks = data.get('data', [])
                for art in artworks:
                    images = art.get('images', {})
                    if not images:
                        continue
                    full_res = images.get('print', {}).get('url') or images.get('web', {}).get('url')
                    if not full_res:
                        continue
                    creators = art.get('creators', [])
                    artist = creators[0].get('description') if creators else 'Unknown Artist'
                    found.append({
                        "source_url": full_res,
                        "thumbnail_url": images.get('web', {}).get('url') or full_res,
                        "proposed_title": art.get('title') or 'Unknown',
                        "proposed_artist": artist,
                        "source_api": "Cleveland Museum of Art",
                        "context_hints": json.dumps(art)
                    })
        except Exception:
            logger.error(f"[Scout] ClevelandArtScout failed: {traceback.format_exc()}")
        return found


class RijksmuseumScout(MuseumScout):
    """
    Scout for the Rijksmuseum (Amsterdam) using the free Open Data Search API.
    Does NOT require an API key.
    Uses Linked Art resolution to extract IIIF image endpoints and metadata.
    """
    SEARCH_URL = "https://data.rijksmuseum.nl/search/collection"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = query or "painting"
        logger.info(f"[Scout] RijksmuseumScout (Open Data) searching for: {q} (intent: {intent.query_type if intent else 'none'})")
        found = []
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                params = {"imageAvailable": "true"}

                if intent and intent.query_type == "artist":
                    # Use 'creator' param for artist queries — returns actual works
                    # BY the artist, not just items mentioning them (posters, etc.)
                    # creator=Vincent van Gogh → 11 works with images
                    # vs description=Vincent van Gogh → 52 items (mostly merch)
                    params["creator"] = intent.canonical_name
                else:
                    # Use description for genre/subject/freetext
                    params["description"] = q

                response = await client.get(self.SEARCH_URL, params=params, timeout=15.0)
                if response.status_code != 200:
                    logger.error(f"[Scout] Rijksmuseum search failed: {response.status_code}")
                    return []

                data = response.json()
                items = data.get('orderedItems', [])
                if not items:
                    return []

                # Apply offset and limit
                selected_items = items[offset:offset + limit]

                for item in selected_items:
                    item_url = item.get('id')
                    if not item_url:
                        continue

                    await asyncio.sleep(0.2)  # B4: space the per-item resolution requests (rate-limit courtesy, like Met)

                    # Ensure we use the 'data' resolver directly to preserve profile params
                    item_url = item_url.replace("id.rijksmuseum.nl", "data.rijksmuseum.nl")

                    # Resolve each item using Dublin Core profile for easy metadata extraction
                    res_params = {"_profile": "dc"}
                    res_headers = {"Accept": "application/ld+json"}
                    res_resp = await client.get(item_url, params=res_params, headers=res_headers, timeout=10.0)

                    if res_resp.status_code != 200:
                        continue

                    item_data = res_resp.json()

                    # Extract IIIF image from 'relation' field
                    relation = item_data.get('relation', {})
                    img_base_url = None
                    if isinstance(relation, dict):
                        img_base_url = relation.get('@id')
                    elif isinstance(relation, list):
                        for r in relation:
                            if isinstance(r, dict) and r.get('@id'):
                                img_base_url = r.get('@id')
                                break

                    if not img_base_url:
                        continue

                    source_url = img_base_url
                    thumb_url = source_url.replace("/full/max/", "/full/400,/")

                    def extract_label(node):
                        if isinstance(node, str):
                            return node
                        if isinstance(node, dict):
                            t_val = node.get('title')
                            if isinstance(t_val, list):
                                for t in t_val:
                                    if isinstance(t, dict) and t.get('@language') == 'en':
                                        return t.get('@value')
                                if t_val:
                                    return t_val[0].get('@value') if isinstance(t_val[0], dict) else t_val[0]
                            return t_val or node.get('@id')
                        return str(node)

                    artist_node = item_data.get('creator', {})
                    artist = extract_label(artist_node) or "Unknown Artist"

                    raw_title = item_data.get('title')

                    found.append({
                        "source_url": source_url,
                        "thumbnail_url": thumb_url,
                        "proposed_title": raw_title or 'Unknown Title',
                        "proposed_artist": artist,
                        "source_api": "Rijksmuseum",
                        "context_hints": json.dumps({
                            "source_lang": "nl",
                            "original_title": raw_title,
                            "raw_metadata": item_data
                        })
                    })
        except Exception:
            logger.error(f"[Scout] RijksmuseumScout failed: {traceback.format_exc()}")
        return found


class SmkScout(MuseumScout):
    """
    Scout for the Statens Museum for Kunst (Denmark).
    Post-filters artist results to ensure artist match.
    """
    API_URL = "https://api.smk.dk/api/v1/art/search/"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = query or "*"
        logger.info(f"[Scout] SmkScout searching for: {q} (intent: {intent.query_type if intent else 'none'}, offset: {offset})")
        found = []
        try:
            async with httpx.AsyncClient() as client:
                params = {
                    "keys": intent.canonical_name if intent and intent.query_type == "artist" else q,
                    "filters": "[has_image:true],[public_domain:true]",
                    "lang": "en",
                    "rows": limit * 3 if intent and intent.query_type == "artist" else limit,
                    "offset": offset,
                }
                response = await client.get(self.API_URL, params=params, timeout=15.0)
                if response.status_code != 200:
                    return []

                data = response.json()
                items = data.get('items', [])
                count = 0
                for item in items:
                    if count >= limit:
                        break
                    image_url = item.get('image_native')
                    if not image_url:
                        iiif_id = item.get('image_iiif_id')
                        if iiif_id:
                            image_url = f"https://iip.smk.dk/iiif/jp2/{iiif_id}/full/max/0/default.jpg"
                    if not image_url:
                        continue

                    artist = "Unknown Artist"
                    production = item.get('production', [])
                    if production and isinstance(production, list):
                        artist = production[0].get('creator', artist)

                    # Post-filter: for artist queries, ensure artist matches
                    if intent and intent.query_type == "artist":
                        artist_lower = artist.lower()
                        query_lower = intent.original_query.lower()
                        # Check if query terms appear in artist name
                        if not any(term in artist_lower for term in query_lower.split()):
                            continue

                    found.append({
                        "source_url": image_url,
                        "thumbnail_url": item.get('image_thumbnail') or image_url,
                        "proposed_title": item.get('titles', [{}])[0].get('title') or 'Unknown',
                        "proposed_artist": artist,
                        "source_api": "Statens Museum for Kunst (Denmark)",
                        "context_hints": json.dumps(item)
                    })
                    count += 1
        except Exception:
            logger.error(f"[Scout] SmkScout failed: {traceback.format_exc()}")
        return found


# ---------------------------------------------------------------------------
# Premium (Tier-2) Scouts — Unchanged for now, future optimization pass
# ---------------------------------------------------------------------------

class HarvardScout(MuseumScout):
    """Tier-2 Scout for Harvard Art Museums requiring API key."""
    API_URL = "https://api.harvardartmuseums.org/object"

    def __init__(self, api_key: str = None):
        self.api_key = api_key

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        if not self.api_key:
            return []

        logger.info(f"[Scout] HarvardScout searching for: {query or 'public domain'}")
        found = []
        try:
            async with httpx.AsyncClient() as client:
                params = {
                    "apikey": self.api_key,
                    "keyword": query or "public domain",
                    "hasimage": 1,
                    "size": limit,
                    "page": (offset // limit) + 1 if query else random.randint(1, 10),
                    "fields": "id,title,people,images,century,culture,medium,dimensions,creditline"
                }
                response = await client.get(self.API_URL, params=params, timeout=15.0)
                if response.status_code != 200:
                    return []

                data = response.json()
                artworks = data.get('records', [])
                for art in artworks:
                    images = art.get('images', [])
                    if not images:
                        continue
                    img = images[0].get('baseimageurl')
                    if not img:
                        continue

                    found.append({
                        "source_url": f"{img}?width=2000&apikey={self.api_key}",
                        "thumbnail_url": f"{img}?width=400&apikey={self.api_key}",
                        "proposed_title": art.get('title') or 'Unknown',
                        "proposed_artist": art.get('people', [{}])[0].get('name', 'Unknown') if art.get('people') else 'Unknown',
                        "source_api": "Harvard Art Museums",
                        "context_hints": json.dumps(art)
                    })
        except Exception as e:
            logger.error(f"[Scout] Harvard error: {e}", exc_info=True)
        return found


class SmithsonianScout(MuseumScout):
    """Tier-2 Scout for Smithsonian Open Access requiring API key."""
    API_URL = "https://api.si.edu/openaccess/api/v1.0/search"

    def __init__(self, api_key: str = None):
        self.api_key = api_key

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        if not self.api_key:
            return []

        logger.info(f"[Scout] Smithsonian searching for: {query or 'art'}")
        found = []
        try:
            async with httpx.AsyncClient() as client:
                params = {
                    "api_key": self.api_key,
                    "q": f"{query or 'art'} AND online_media_type:Images",
                    "rows": limit,
                    "start": offset if query else random.randint(0, 100)
                }
                response = await client.get(self.API_URL, params=params, timeout=15.0)
                if response.status_code != 200:
                    return []

                rows = response.json().get('response', {}).get('rows', [])
                for art in rows:
                    content = art.get('content', {})
                    descriptive = content.get('descriptiveNonRepeating', {})

                    images = descriptive.get('online_media', {}).get('media', [])
                    if not images:
                        continue

                    img = [i for i in images if i.get('type') == 'Images']
                    if not img:
                        continue
                    img_url = img[0].get('content')
                    if not img_url:
                        continue

                    freetext = content.get('freetext', {})
                    creators = freetext.get('name', [])
                    artist = creators[0].get('content', 'Unknown') if creators else 'Unknown'

                    found.append({
                        "source_url": img_url,
                        "thumbnail_url": img[0].get('thumbnail', img_url),
                        "proposed_title": art.get('title') or 'Unknown Smithsonian Object',
                        "proposed_artist": artist,
                        "source_api": f"Smithsonian {descriptive.get('data_source', '')}",
                        "context_hints": json.dumps(art)
                    })
        except Exception as e:
            logger.error(f"[Scout] Smithsonian error: {e}", exc_info=True)
        return found


class EuropeanaScout(MuseumScout):
    """
    Tier-2 Scout for Europeana requiring API WSKey.

    Quality filters:
    - contentTier:3/4 — high-quality records with good images
    - IMAGE_SIZE:large/extra_large — decent resolution images
    - what:painting — restricts to paintings for artist searches
    - profile=rich — returns fuller metadata
    """
    API_URL = "https://api.europeana.eu/record/v2/search.json"

    def __init__(self, api_key: str = None):
        self.api_key = api_key

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        if not self.api_key:
            return []

        q = query or "painting"
        logger.info(f"[Scout] Europeana searching for: {q} (intent: {intent.query_type if intent else 'none'}, offset: {offset})")
        found = []
        try:
            async with httpx.AsyncClient() as client:
                # Base params shared across all strategies
                base_params = {
                    "wskey": self.api_key,
                    "reusability": "open",
                    "rows": limit,
                    "media": True,
                    "start": offset + 1,
                    "profile": "rich",
                }

                # Strategy: progressive fallback for best quality results
                # For artists: who: field returns actual works BY the artist
                # For genres: text search + TYPE:IMAGE
                if intent and intent.query_type == "artist":
                    strategies = [
                        # 1st: who: field — small but precise (genuine works)
                        {
                            "query": f'who:"{intent.canonical_name}"',
                            "qf": ["TYPE:IMAGE"],
                        },
                        # 2nd: text + painting — broader, includes glass slides of paintings
                        {
                            "query": f'"{intent.canonical_name}" painting',
                            "qf": ["TYPE:IMAGE"],
                        },
                    ]
                else:
                    strategies = [
                        {
                            "query": q,
                            "qf": ["TYPE:IMAGE"],
                        },
                    ]

                items = []
                for strategy in strategies:
                    params = {
                        **base_params,
                        "query": strategy["query"],
                        "qf": strategy["qf"],
                    }
                    response = await client.get(self.API_URL, params=params, timeout=15.0)
                    if response.status_code != 200:
                        logger.warning(f"[Scout] Europeana returned {response.status_code} for query: {strategy['query']}")
                        continue

                    data = response.json()
                    items = data.get('items', [])
                    total = data.get('totalResults', 0)
                    logger.info(f"[Scout] Europeana strategy '{strategy['query']}' → {len(items)} items (total: {total})")
                    if items:
                        break  # Got results, stop trying fallbacks

                if not items:
                    logger.info("[Scout] Europeana: no results from any strategy")
                    return []

                for art in items:
                    # Prefer edmIsShownBy (full res) over edmPreview (thumbnail)
                    full_url = None
                    if art.get('edmIsShownBy'):
                        full_url = art['edmIsShownBy'][0]

                    thumb_url = None
                    if art.get('edmPreview'):
                        thumb_url = art['edmPreview'][0]

                    if not full_url and not thumb_url:
                        continue

                    img_url = full_url or thumb_url

                    artist = 'Unknown'
                    if art.get('dcCreator'):
                        artist = art['dcCreator'][0]
                    elif art.get('dcContributor'):
                        artist = art['dcContributor'][0]

                    title = art.get('title', ['Unknown Europeana Asset'])[0]

                    found.append({
                        "source_url": img_url,
                        "thumbnail_url": thumb_url or img_url,
                        "proposed_title": title,
                        "proposed_artist": artist,
                        "source_api": "Europeana",
                        "context_hints": json.dumps({
                            "provider": (art.get('dataProvider') or ['Unknown'])[0],
                            "country": (art.get('country') or ['Unknown'])[0],
                            "year": (art.get('year') or ['Unknown'])[0],
                            "rights": (art.get('rights') or ['Unknown'])[0],
                            "edmIsShownAt": (art.get('edmIsShownAt') or [None])[0],
                        })
                    })
        except Exception as e:
            logger.error(f"[Scout] Europeana error: {e}", exc_info=True)
        logger.info(f"[Scout] Europeana returning {len(found)} results")
        return found


# ---------------------------------------------------------------------------
# Keyless open-collection Scouts (NASA, Wikimedia Commons)
# ---------------------------------------------------------------------------

class NasaScout(MuseumScout):
    """
    Scout for the NASA images library (images-api.nasa.gov). Keyless, public domain.
    Resolves each search hit to its real full-resolution asset via the item's
    collection.json manifest (the search 'links' only expose a thumbnail).
    """
    SEARCH_URL = "https://images-api.nasa.gov/search"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = (intent.canonical_name if intent and intent.query_type == "artist" else query) or "galaxy"
        logger.info(f"[Scout] NasaScout searching for: {q} (offset: {offset})")
        found = []
        headers = {"User-Agent": "Pieria/1.0"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                resp = await client.get(self.SEARCH_URL, params={"q": q, "media_type": "image"})
                if resp.status_code != 200:
                    logger.error(f"[Scout] NASA search returned {resp.status_code}")
                    return []
                items = resp.json().get("collection", {}).get("items", [])
                selected = items[offset:offset + limit]
                for it in selected:
                    d = (it.get("data") or [{}])[0]
                    links = it.get("links") or []
                    thumb = links[0].get("href") if links else None
                    if not thumb:
                        continue
                    full = await _nasa_best_asset(client, d.get("nasa_id", "")) or thumb
                    found.append({
                        "source_url": full,
                        "thumbnail_url": thumb,
                        "proposed_title": d.get("title") or "Untitled",
                        "proposed_artist": (f"NASA — {d.get('center')}" if d.get("center") else "NASA"),
                        "source_api": "NASA",
                        "context_hints": json.dumps(d),
                    })
        except Exception:
            logger.error(f"[Scout] NasaScout failed: {traceback.format_exc()}")
        logger.info(f"[Scout] NASA: returning {len(found)} artworks")
        return found


class WikimediaScout(MuseumScout):
    """
    Scout for Wikimedia Commons (commons.wikimedia.org). Keyless. Gates hard, in-path,
    on what the live pipeline cannot gate later: public-domain/CC0 license AND a real
    raster image at least MIN_DISPLAY_EDGE px on the long edge (display-grade). Politely
    rate-limited via the shared _wm_throttle.
    """
    API_URL = "https://commons.wikimedia.org/w/api.php"

    async def find_art(self, query: str = None, intent: SearchIntent = None,
                       offset: int = 0, limit: int = 10) -> List[Dict]:
        q = (intent.canonical_name if intent and intent.query_type == "artist" else query) or "painting"
        logger.info(f"[Scout] WikimediaScout searching for: {q} (offset: {offset})")
        found = []
        params = {
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": q, "gsrnamespace": "6",
            "gsrlimit": str(limit), "gsroffset": str(offset),
            "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
        }
        await _wm_throttle()
        try:
            async with httpx.AsyncClient(headers={"User-Agent": "Pieria/1.0 (https://github.com/pieria-art/Pieria)"}, timeout=30.0) as client:
                resp = None
                for attempt in range(3):
                    resp = await client.get(self.API_URL, params=params)
                    if resp.status_code == 429:
                        await asyncio.sleep(3 * (attempt + 1)); continue
                    break
                if not resp or resp.status_code != 200:
                    logger.error(f"[Scout] Wikimedia returned {resp.status_code if resp else 'no response'}")
                    return []
                pages = (resp.json().get("query") or {}).get("pages") or {}
        except Exception:
            logger.error(f"[Scout] WikimediaScout failed: {traceback.format_exc()}")
            return []

        for p in pages.values():
            ii = (p.get("imageinfo") or [{}])[0]
            mime = ii.get("mime", "")
            if not mime.startswith("image/") or "svg" in mime:
                continue
            if max(ii.get("width") or 0, ii.get("height") or 0) < MIN_DISPLAY_EDGE:
                continue
            extmeta = ii.get("extmetadata") or {}
            if not _wm_is_pd(extmeta):
                continue
            page_title = p.get("title", "")  # "File:Foo.jpg"
            fname = page_title.split("File:", 1)[-1]
            if not fname:
                continue
            artist = _strip_html((extmeta.get("Artist") or {}).get("value", "")) or "Unknown Artist"
            found.append({
                "source_url": _wikimedia_filepath(fname, 3840),   # up to 4K (capped at the original)
                "thumbnail_url": _wikimedia_filepath(fname, 600),
                "proposed_title": _wm_clean_title((extmeta.get("ObjectName") or {}).get("value", ""), fname),
                "proposed_artist": artist,
                "source_api": "Wikimedia Commons",
                "context_hints": json.dumps({
                    "license": (extmeta.get("LicenseShortName") or {}).get("value", ""),
                    "width": ii.get("width"), "height": ii.get("height"),
                    "descriptionurl": ii.get("descriptionurl"),
                }),
            })
        logger.info(f"[Scout] Wikimedia: returning {len(found)} artworks")
        return found


def _title_from_filename(fn: str) -> str:
    stem = fn.rsplit(".", 1)[0]
    return stem.replace("_", " ").strip()


def _wm_clean_title(raw: str, fname: str) -> str:
    """Commons ObjectName often embeds Wikibase structured values
    (e.g. 'The Starry Nighttitle QS:P1476,en:"The Starry Night"'). Strip to a plain label,
    falling back to the filename when nothing clean remains."""
    t = re.split(r"\s*(?:title\s*)?QS:", _strip_html(raw or ""))[0].strip()
    return t or _title_from_filename(fname)


# ---------------------------------------------------------------------------
# Search Session State — In-memory store for "Load More" pagination
# ---------------------------------------------------------------------------

@dataclass
class SearchSession:
    """Tracks state for a paginated search session."""
    session_id: str
    query: str
    intent: SearchIntent
    sources: List[str]
    offset: int = 0
    limit: int = 10
    created_at: datetime = field(default_factory=datetime.utcnow)

# In-memory session store — survives as long as the server runs
_search_sessions: Dict[str, SearchSession] = {}
SESSION_TTL_MINUTES = 30


def create_search_session(query: str, intent: SearchIntent, sources: List[str], limit: int = 10) -> SearchSession:
    """Create a new search session and return it."""
    _cleanup_expired_sessions()
    session = SearchSession(
        session_id=str(uuid.uuid4()),
        query=query,
        intent=intent,
        sources=sources,
        offset=0,
        limit=limit,
    )
    _search_sessions[session.session_id] = session
    logger.info(f"[Session] Created search session {session.session_id[:8]}... "
                f"(query='{query}', sources={sources}, limit={limit})")
    return session


def get_search_session(session_id: str) -> Optional[SearchSession]:
    """Retrieve an active session by ID, or None if expired/not found."""
    _cleanup_expired_sessions()
    return _search_sessions.get(session_id)


def _cleanup_expired_sessions():
    """Remove sessions older than TTL."""
    now = datetime.utcnow()
    expired = [
        sid for sid, session in _search_sessions.items()
        if (now - session.created_at) > timedelta(minutes=SESSION_TTL_MINUTES)
    ]
    for sid in expired:
        del _search_sessions[sid]
        logger.info(f"[Session] Expired session {sid[:8]}...")


# ---------------------------------------------------------------------------
# Scout Dispatcher
# ---------------------------------------------------------------------------

async def run_scouts(db: Session, query: str = None, sources: List[str] = None,
                     intent: SearchIntent = None, offset: int = 0, limit: int = 10) -> List[Dict]:
    """
    Runs selected active scouts and returns results (without inserting into DB).
    The caller is responsible for insertion.
    """
    settings = db.query(SettingsModel).all()
    keys = {s.setting_key: s.setting_value for s in settings}

    all_scouts = {
        "chicago": ChicagoArtScout(),
        "met": MetMuseumScout(),
        "cleveland": ClevelandArtScout(),
        "rijks": RijksmuseumScout(),
        "smk": SmkScout(),
        "nasa": NasaScout(),
        "wikimedia": WikimediaScout(),
        "harvard": HarvardScout(keys.get("harvard_api_key")),
        "smithsonian": SmithsonianScout(keys.get("smithsonian_api_key")),
        "europeana": EuropeanaScout(keys.get("europeana_api_key"))
    }

    active_scouts = []
    if sources:
        for s in sources:
            if s in all_scouts:
                active_scouts.append(all_scouts[s])
    else:
        active_scouts = list(all_scouts.values())

    if not active_scouts:
        return []

    tasks = [scout.find_art(query=query, intent=intent, offset=offset, limit=limit) for scout in active_scouts]
    results_lists = await asyncio.gather(*tasks)

    # Flatten results
    all_results = []
    for results in results_lists:
        all_results.extend(results)

    logger.info(f"[Scout] Scouts returned {len(all_results)} total items across {len(active_scouts)} sources.")
    return all_results
