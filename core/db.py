"""
Database layer for SERM Distribution Engine (SQLite).
"""
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path

import yaml

from config.settings import DB_PATH
from core.models import Campaign, Article, IndexingResult

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    brand_name TEXT NOT NULL,
    target_query TEXT NOT NULL,
    seed_topic TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    lead TEXT,
    content_markdown TEXT NOT NULL,
    content_html TEXT,
    tags TEXT,
    cover_image_path TEXT,
    status TEXT NOT NULL,
    published_url TEXT,
    published_at TIMESTAMP,
    error_message TEXT,
    FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS indexing_logs (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    success INTEGER NOT NULL,
    message TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS bot_events (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    event_type TEXT NOT NULL,
    slug TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Lead status: one row per person, written only by the owner (lead status buttons in Telegram).
-- *_at of a stage = when it was FIRST reached (COALESCE), so the funnel to money stays monotonic.
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    slug TEXT NOT NULL,
    interest TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    contacted_at TIMESTAMP,
    call_set_at TIMESTAMP,
    call_done_at TIMESTAMP,
    closed_at TIMESTAMP,
    product TEXT,
    amount INTEGER,
    reminded_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_campaign ON articles(campaign_id);
CREATE INDEX IF NOT EXISTS idx_articles_platform ON articles(platform);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_bot_events_user ON bot_events(user_id);
CREATE INDEX IF NOT EXISTS idx_bot_events_type ON bot_events(event_type);
CREATE INDEX IF NOT EXISTS idx_bot_events_slug ON bot_events(slug);
CREATE INDEX IF NOT EXISTS idx_leads_slug ON leads(slug);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
"""

# PRAGMA user_version 0 -> 1: leads of the clicks made before the leads table existed (first tier_click of each
# person). reminded_status = 'new': history is reachable through /leads, the owner is not flooded with reminders.
BACKFILL_LEADS_SQL = """
INSERT OR IGNORE INTO leads (user_id, username, first_name, slug, interest, status, created_at, status_at,
                             reminded_status)
SELECT e.user_id, e.username, e.first_name, COALESCE(NULLIF(TRIM(e.slug), ''), 'general'), e.details, 'new',
       e.created_at, e.created_at, 'new'
FROM bot_events e
WHERE e.event_type = 'tier_click'
  AND e.rowid = (SELECT f.rowid FROM bot_events f
                 WHERE f.user_id = e.user_id AND f.event_type = 'tier_click'
                 ORDER BY f.created_at, f.rowid LIMIT 1)
"""
SCHEMA_VERSION = 1

# Stage columns set_lead_status may stamp (never interpolated from outside this list)
LEAD_STAGE_COLUMNS = ("contacted_at", "call_set_at", "call_done_at", "closed_at")
LEAD_CLOSED_STATUSES = ("sale", "no_sale")

ANALYTICS_TEXTS = "analytics/recommendations.yaml"
SAMPLE_STATUS_TEXTS = "analytics/sample_status.yaml"


class _AnalyticsTexts:
    """Analytics wording from the profile templates: YAML parsed first, then each used string is
    rendered as Jinja with its own context (slugs come from any Telegram user and cannot break the YAML)."""

    def __init__(self, name: str = ANALYTICS_TEXTS):
        from core.profile import get_profile
        from core.templating import get_environment
        self.profile = get_profile()
        self.env = get_environment(self.profile)
        source, _, _ = self.env.loader.get_source(self.env, name)
        self.data = yaml.safe_load(source)

    def fill(self, text: str, **ctx: Any) -> str:
        return self.env.from_string(text).render(profile=self.profile, **ctx)

    def item(self, key: str, rec_type: str, priority: str, **ctx: Any) -> Dict[str, Any]:
        entry = self.data[key]
        return {"type": rec_type, "priority": priority,
                "title": self.fill(entry["title"], **ctx), "text": self.fill(entry["text"], **ctx)}

    def topic_names(self) -> Dict[str, str]:
        # Order matters: unknown slugs are matched by substring in this order
        names = {m.slug: m.topic_name for m in self.profile.lead_magnets if m.topic_name}
        names.setdefault("general", self.fill(self.data["topic_general"]))
        return names


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
        # One transaction: the backfill and the version bump land together or not at all
        with self._conn() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                conn.execute(BACKFILL_LEADS_SQL)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # --- Campaigns ---
    def add_campaign(self, campaign: Campaign):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO campaigns (id, brand_name, target_query, seed_topic, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (campaign.id, campaign.brand_name, campaign.target_query, campaign.seed_topic, campaign.created_at)
            )

    def get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not row:
                return None
            return Campaign(
                id=row["id"],
                brand_name=row["brand_name"],
                target_query=row["target_query"],
                seed_topic=row["seed_topic"],
                created_at=str(row["created_at"])
            )

    def list_campaigns(self) -> List[Campaign]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
            return [
                Campaign(
                    id=r["id"],
                    brand_name=r["brand_name"],
                    target_query=r["target_query"],
                    seed_topic=r["seed_topic"],
                    created_at=str(r["created_at"])
                )
                for r in rows
            ]

    # --- Articles ---
    def add_article(self, article: Article):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO articles (
                    id, campaign_id, platform, title, lead, content_markdown,
                    content_html, tags, cover_image_path, status, published_url,
                    published_at, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article.id, article.campaign_id, article.platform, article.title,
                    article.lead, article.content_markdown, article.content_html,
                    article.tags, article.cover_image_path, article.status,
                    article.published_url, article.published_at, article.error_message
                )
            )

    def get_article(self, article_id: str) -> Optional[Article]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
            if not row:
                return None
            return self._row_to_article(row)

    def list_articles_by_campaign(self, campaign_id: str) -> List[Article]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM articles WHERE campaign_id = ?", (campaign_id,)).fetchall()
            return [self._row_to_article(r) for r in rows]

    def update_article_status(
        self,
        article_id: str,
        status: str,
        published_url: Optional[str] = None,
        published_at: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE articles
                SET status = ?,
                    published_url = COALESCE(?, published_url),
                    published_at = COALESCE(?, published_at),
                    error_message = ?
                WHERE id = ?
                """,
                (status, published_url, published_at, error_message, article_id)
            )

    def _row_to_article(self, row: sqlite3.Row) -> Article:
        return Article(
            id=row["id"],
            campaign_id=row["campaign_id"],
            platform=row["platform"],
            title=row["title"],
            lead=row["lead"] or "",
            content_markdown=row["content_markdown"],
            content_html=row["content_html"] or "",
            tags=row["tags"] or "",
            cover_image_path=row["cover_image_path"],
            status=row["status"],
            published_url=row["published_url"],
            published_at=str(row["published_at"]) if row["published_at"] else None,
            error_message=row["error_message"]
        )

    # --- Indexing Logs ---
    def log_indexing(self, result: IndexingResult):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO indexing_logs (id, article_id, channel, success, message, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), result.article_id, result.channel, 1 if result.success else 0, result.message, result.timestamp)
            )

    def get_indexing_logs(self, article_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM indexing_logs WHERE article_id = ? ORDER BY timestamp DESC", (article_id,)).fetchall()
            return [dict(r) for r in rows]

    # --- Conversion & Bot Analytics ---
    def log_bot_event(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        event_type: str,
        slug: Optional[str] = None,
        details: Optional[str] = None
    ) -> str:
        event_id = str(uuid.uuid4())
        clean_slug = (slug or "general").strip()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO bot_events (id, user_id, username, first_name, event_type, slug, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (event_id, user_id, username, first_name, event_type, clean_slug, details)
            )
        return event_id

    def get_funnel_summary(self) -> Dict[str, Any]:
        with self._conn() as conn:
            total_events = conn.execute("SELECT COUNT(*) FROM bot_events").fetchone()[0]
            unique_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM bot_events").fetchone()[0]
            unique_starts = conn.execute("SELECT COUNT(DISTINCT user_id) FROM bot_events WHERE event_type = 'start'").fetchone()[0]
            unique_subs = conn.execute("SELECT COUNT(DISTINCT user_id) FROM bot_events WHERE event_type = 'subscribed'").fetchone()[0]
            unique_pdfs = conn.execute("SELECT COUNT(DISTINCT user_id) FROM bot_events WHERE event_type = 'pdf_delivered'").fetchone()[0]
            unique_leads = conn.execute("SELECT COUNT(DISTINCT user_id) FROM bot_events WHERE event_type = 'tier_click'").fetchone()[0]

            cr_sub = round((unique_subs / unique_starts * 100), 1) if unique_starts > 0 else 0.0
            cr_pdf = round((unique_pdfs / unique_starts * 100), 1) if unique_starts > 0 else 0.0
            cr_lead = round((unique_leads / unique_starts * 100), 1) if unique_starts > 0 else 0.0

            # After the lead click: what the owner marked in Telegram (leads table)
            lead_row = conn.execute("""
                SELECT COUNT(contacted_at), COUNT(call_set_at), COUNT(call_done_at),
                       COUNT(CASE WHEN status = 'sale' THEN 1 END),
                       COALESCE(SUM(CASE WHEN status = 'sale' THEN amount END), 0)
                FROM leads
            """).fetchone()
            unique_contacted, unique_calls_set, unique_calls_done, unique_sales, revenue = lead_row
            cr_sale = round((unique_sales / unique_starts * 100), 1) if unique_starts > 0 else 0.0
            cr_lead_to_sale = round((unique_sales / unique_leads * 100), 1) if unique_leads > 0 else 0.0

            # Статистическая выборка: ориентир 50–100 уникальных переходов
            sample_min = 50
            sample_target = 100
            sample_progress = min(100.0, round((unique_starts / sample_target) * 100, 1))

            if unique_starts < sample_min:
                sample_stage = "calibrating"
                sample_color = "#f59e0b"
            elif unique_starts < sample_target:
                sample_stage = "emerging"
                sample_color = "#3b82f6"
            else:
                sample_stage = "significant"
                sample_color = "#10b981"
            texts = _AnalyticsTexts(SAMPLE_STATUS_TEXTS)
            stage_ctx = {"starts": unique_starts, "sample_min": sample_min, "sample_target": sample_target}
            sample_status = texts.fill(texts.data[sample_stage]["status"], **stage_ctx)
            sample_description = texts.fill(texts.data[sample_stage]["description"], **stage_ctx)

            return {
                "total_events": total_events,
                "unique_users": unique_users,
                "unique_starts": unique_starts,
                "unique_subscribers": unique_subs,
                "unique_pdfs": unique_pdfs,
                "unique_leads": unique_leads,
                "cr_sub": cr_sub,
                "cr_pdf": cr_pdf,
                "cr_lead": cr_lead,
                "sample_min": sample_min,
                "sample_target": sample_target,
                "sample_progress": sample_progress,
                "sample_stage": sample_stage,
                "sample_status": sample_status,
                "sample_description": sample_description,
                "sample_color": sample_color,
                "unique_contacted": unique_contacted,
                "unique_calls_set": unique_calls_set,
                "unique_calls_done": unique_calls_done,
                "unique_sales": unique_sales,
                "revenue": revenue,
                "cr_sale": cr_sale,
                "cr_lead_to_sale": cr_lead_to_sale
            }

    def get_slug_breakdown(self) -> List[Dict[str, Any]]:
        texts = _AnalyticsTexts()
        TOPIC_NAMES = texts.topic_names()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT 
                    COALESCE(NULLIF(slug, ''), 'general') as slug,
                    COUNT(DISTINCT CASE WHEN event_type = 'start' THEN user_id END) as starts,
                    COUNT(DISTINCT CASE WHEN event_type = 'subscribed' THEN user_id END) as subs,
                    COUNT(DISTINCT CASE WHEN event_type = 'pdf_delivered' THEN user_id END) as pdfs,
                    COUNT(DISTINCT CASE WHEN event_type = 'tier_click' THEN user_id END) as leads
                FROM bot_events
                GROUP BY COALESCE(NULLIF(slug, ''), 'general')
                ORDER BY starts DESC, subs DESC
            """).fetchall()
            # Separate query over leads, merged by slug: the bot_events counts above stay exactly as they were
            lead_stats = {r["slug"]: r for r in conn.execute("""
                SELECT slug,
                       COUNT(contacted_at) as contacted,
                       COUNT(call_set_at) as calls_set,
                       COUNT(call_done_at) as calls_done,
                       COUNT(CASE WHEN status = 'sale' THEN 1 END) as sales,
                       COALESCE(SUM(CASE WHEN status = 'sale' THEN amount END), 0) as revenue
                FROM leads
                GROUP BY slug
            """).fetchall()}

            result = []
            for r in rows:
                s = r["slug"]
                starts = r["starts"]
                subs = r["subs"]
                pdfs = r["pdfs"]
                leads = r["leads"]
                cr_sub = round((subs / starts * 100), 1) if starts > 0 else 0.0
                cr_lead = round((leads / starts * 100), 1) if starts > 0 else 0.0

                if cr_lead >= 20 or cr_sub >= 60:
                    efficiency = "high"
                elif cr_sub >= 35 or cr_lead >= 10:
                    efficiency = "medium"
                else:
                    efficiency = "low"

                matched_name = TOPIC_NAMES.get(s)
                if not matched_name:
                    for k, v in TOPIC_NAMES.items():
                        if k in s:
                            matched_name = v
                            break
                if not matched_name:
                    matched_name = texts.fill(texts.data["topic_fallback"], slug=s)

                result.append({
                    "slug": s,
                    "topic_name": matched_name,
                    "starts": starts,
                    "subs": subs,
                    "cr_sub": cr_sub,
                    "pdfs": pdfs,
                    "leads": leads,
                    "cr_lead": cr_lead,
                    "efficiency": efficiency,
                    **{key: (lead_stats[s][key] if s in lead_stats else 0)
                       for key in ("contacted", "calls_set", "calls_done", "sales", "revenue")}
                })
            return result

    def last_start_slug(self, user_id: int) -> Optional[str]:
        """Slug of the user's latest /start: restores the visit source after a bot restart."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT slug FROM bot_events WHERE user_id = ? AND event_type = 'start' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (user_id,)
            ).fetchone()
        return row[0] if row else None

    # --- Leads (status set by the owner in Telegram) ---
    def upsert_lead(self, user_id: int, username: Optional[str], first_name: Optional[str],
                    slug: Optional[str], interest: Optional[str]) -> int:
        """Lead id of this person; created on the first lead click. Slug and created_at keep the FIRST attribution."""
        clean_slug = (slug or "general").strip() or "general"
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO leads (user_id, username, first_name, slug, interest)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, username, first_name, clean_slug, interest)
            )
            return conn.execute("SELECT id FROM leads WHERE user_id = ?", (user_id,)).fetchone()[0]

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return dict(row) if row else None

    def set_lead_status(self, lead_id: int, expected_status: str, new_status: str,
                        stage_column: Optional[str] = None, product: Optional[str] = None) -> bool:
        """Atomic transition: applies only while the lead is still in expected_status (a double tap or a stale
        button changes nothing). A stage column is stamped once; product only when given."""
        if stage_column is not None and stage_column not in LEAD_STAGE_COLUMNS:
            raise ValueError(f"unknown lead stage column: {stage_column}")
        stage_sql = f", {stage_column} = COALESCE({stage_column}, CURRENT_TIMESTAMP)" if stage_column else ""
        with self._conn() as conn:
            cur = conn.execute(
                f"""
                UPDATE leads
                SET status = ?, status_at = CURRENT_TIMESTAMP, reminded_status = NULL,
                    product = COALESCE(?, product){stage_sql}
                WHERE id = ? AND status = ?
                """,
                (new_status, product, lead_id, expected_status)
            )
            return cur.rowcount == 1

    def set_lead_amount(self, lead_id: int, amount: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE leads SET amount = ? WHERE id = ? AND status = 'sale'", (amount, lead_id))
            return cur.rowcount == 1

    def list_leads(self, limit: int = 50, open_only: bool = False) -> List[Dict[str, Any]]:
        where = f"WHERE status NOT IN ({', '.join('?' for _ in LEAD_CLOSED_STATUSES)})" if open_only else ""
        params = (*LEAD_CLOSED_STATUSES, limit) if open_only else (limit,)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM leads {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params
            ).fetchall()
            return [dict(r) for r in rows]

    def due_lead_reminders(self, hours_by_status: Dict[str, int], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Leads sitting in a reminded status longer than its threshold and not yet reminded about it."""
        now = now or datetime.now(timezone.utc)
        due = []
        with self._conn() as conn:
            for status, hours in hours_by_status.items():
                # status_at is CURRENT_TIMESTAMP text (UTC, "YYYY-MM-DD HH:MM:SS"): compare as the same text
                cutoff = (now.astimezone(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                rows = conn.execute(
                    """
                    SELECT * FROM leads
                    WHERE status = ? AND status_at <= ? AND reminded_status IS NOT status
                    ORDER BY status_at, id
                    """,
                    (status, cutoff)
                ).fetchall()
                due.extend(dict(r) for r in rows)
        return due

    def mark_reminded(self, lead_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE leads SET reminded_status = ? WHERE id = ? AND status = ?", (status, lead_id, status))

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, username, first_name, event_type, slug, details, created_at
                FROM bot_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_content_strategy_recommendations(self) -> List[Dict[str, Any]]:
        summary = self.get_funnel_summary()
        breakdown = self.get_slug_breakdown()
        starts = summary["unique_starts"]
        texts = _AnalyticsTexts()

        recs = []
        if starts == 0:
            for entry in texts.data["launch"]:
                recs.append({"type": entry["type"], "priority": entry["priority"],
                             "title": texts.fill(entry["title"]), "text": texts.fill(entry["text"])})
            return recs

        # Оценка готовности выборки
        if starts < 50:
            recs.append(texts.item("sample_accumulation", "sample_accumulation", "high", starts=starts))
        elif starts < 100:
            recs.append(texts.item("sample_emerging", "sample_emerging", "high", starts=starts))
        else:
            recs.append(texts.item("sample_complete", "sample_complete", "high", starts=starts))

        if breakdown:
            best_lead_slug = max(breakdown, key=lambda x: (x["cr_lead"], x["leads"]))
            best_sub_slug = max(breakdown, key=lambda x: (x["cr_sub"], x["subs"]))

            if best_sub_slug["subs"] > 0:
                recs.append(texts.item("scaling", "scaling", "high" if starts >= 50 else "medium",
                                       starts=starts, best=best_sub_slug))

            if best_lead_slug["leads"] > 0:
                recs.append(texts.item("monetization", "monetization", "high", starts=starts, best=best_lead_slug))

            low_cr = [b for b in breakdown if b["starts"] >= 10 and b["cr_sub"] < 25]
            if low_cr and starts >= 50:
                for low in low_cr:
                    recs.append(texts.item("optimization", "optimization", "medium", starts=starts, low=low))

        return recs
