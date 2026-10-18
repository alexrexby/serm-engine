"""
Domain data models for SERM Distribution & Fast Indexing.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any

@dataclass
class Campaign:
    id: str
    brand_name: str
    target_query: str
    seed_topic: str
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

@dataclass
class Article:
    id: str
    campaign_id: str
    platform: str
    title: str
    lead: str
    content_markdown: str
    content_html: str
    tags: str
    cover_image_path: Optional[str] = None
    status: str = "draft"  # draft, ready, scheduled, publishing, published, submitted, failed
    published_url: Optional[str] = None
    published_at: Optional[str] = None
    error_message: Optional[str] = None

@dataclass
class PublishResult:
    success: bool
    platform: str
    article_id: str
    published_url: Optional[str] = None
    status: str = "published"  # "published" (instant) or "submitted" (under review)
    message: str = ""
    error: Optional[str] = None

@dataclass
class IndexingResult:
    article_id: str
    channel: str  # telegram, vk, indexnow
    success: bool
    message: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
