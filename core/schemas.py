"""Shared Pydantic response schemas.

ArtworkSchema is the one cross-cutting schema: it's the response_model for routes in
routers/library.py, routers/curation.py, AND studio routes that stay in app.py (upload/personal,
studio/photo). Living here lets all three import it without routers/* ever importing from `app`.
"""

from typing import Optional

from pydantic import BaseModel


class ArtworkSchema(BaseModel):
    id: int
    filename: str
    original_width: int
    original_height: int
    title: Optional[str] = None
    agent_name: Optional[str] = None
    agent_role: Optional[str] = None
    creation_date: Optional[str] = None; cultural_context: Optional[str] = None
    medium: Optional[str] = None; date_display: Optional[str] = None
    description_narrative: Optional[str] = None; tags: Optional[str] = None
    status: str
    crop_x: float
    crop_y: float
    crop_width: float
    crop_height: float
    focal_x: float = 0.5
    focal_y: float = 0.5
    is_personal: bool = False
    model_config = {"from_attributes": True}
