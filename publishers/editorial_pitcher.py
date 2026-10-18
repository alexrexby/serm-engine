"""
Editorial Pitching & Submission Pipeline for High-Trust Media:
- T-Bank Секреты (Telegram bot @bs_community_bot)
- Biz360.ru (Editorial feedback form / email)
- RB.ru (Columinist Tilda form #form764023213)
"""
import os
import json
from pathlib import Path
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from config.settings import EXPORTS_DIR, PLATFORMS_CONFIG

class EditorialPitcher(BasePublisher):
    def __init__(self, platform_name: str):
        super().__init__(platform_name, PLATFORMS_CONFIG.get(platform_name, {}))
        self.exports_dir = EXPORTS_DIR
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def verify_auth(self) -> bool:
        return True

    def interactive_login(self):
        print(f"[{self.platform_name}] Редакционные площадки не требуют веб-авторизации: материалы отправляются через бота или форму.")

    def publish(self, article: Article) -> PublishResult:
        """
        Prepares export files and structured pitch package.
        """
        export_file = self.exports_dir / f"{article.platform}_{article.id[:8]}.md"
        with open(export_file, "w", encoding="utf-8") as f:
            f.write(article.content_markdown)

        if article.platform == "tbank":
            return self._handle_tbank_pitch(article, export_file)
        elif article.platform == "biz360":
            return self._handle_biz360_pitch(article, export_file)
        elif article.platform == "rb":
            return self._handle_rb_pitch(article, export_file)
        else:
            return PublishResult(
                success=True,
                platform=article.platform,
                article_id=article.id,
                status="exported",
                message=f"Файл экспортирован в {export_file}"
            )

    def _handle_tbank_pitch(self, article: Article, export_file: Path) -> PublishResult:
        pitch_text = (
            f"📨 ПИТЧ ДЛЯ РЕДАКЦИИ Т-БАНК «СЕКРЕТЫ БИЗНЕСА» (через бота @bs_community_bot):\n"
            f"--------------------------------------------------------------------\n"
            f"Тема: {article.title}\n"
            f"Суть кейса: {article.lead}\n"
            f"Целевая аудитория: Предприниматели, малый и средний бизнес\n"
            f"Файл с полным текстом: {export_file}\n"
            f"Канал отправки: https://t.me/bs_community_bot (проверка в течение 7 раб. дней)\n"
            f"--------------------------------------------------------------------"
        )
        pitch_file = self.exports_dir / f"pitch_tbank_{article.id[:8]}.txt"
        with open(pitch_file, "w", encoding="utf-8") as f:
            f.write(pitch_text)

        return PublishResult(
            success=True,
            platform="tbank",
            article_id=article.id,
            status="submitted",
            message=f"Сформирован пакет для отправки в @bs_community_bot. Питч сохранен: {pitch_file}"
        )

    def _handle_biz360_pitch(self, article: Article, export_file: Path) -> PublishResult:
        pitch_text = (
            f"📨 ЗАЯВКА В РЕДАКЦИЮ BIZ360.RU (раздел 'Как стать автором'):\n"
            f"--------------------------------------------------------------------\n"
            f"Тема статьи: {article.title}\n"
            f"Краткое описание: {article.lead}\n"
            f"Практическая польза для малого бизнеса: Разбор реального опыта и метрик компании\n"
            f"Файл материала: {export_file}\n"
            f"Форма подачи: https://biz360.ru/about/ (форма обратной связи)\n"
            f"--------------------------------------------------------------------"
        )
        pitch_file = self.exports_dir / f"pitch_biz360_{article.id[:8]}.txt"
        with open(pitch_file, "w", encoding="utf-8") as f:
            f.write(pitch_text)

        return PublishResult(
            success=True,
            platform="biz360",
            article_id=article.id,
            status="submitted",
            message=f"Сформирован пакет для отправки в Biz360. Питч сохранен: {pitch_file}"
        )

    def _handle_rb_pitch(self, article: Article, export_file: Path) -> PublishResult:
        pitch_text = (
            f"📨 ЗАЯВКА НА КОЛОНКУ ЭКСПЕРТА RB.RU (Форма #rec764023213):\n"
            f"--------------------------------------------------------------------\n"
            f"Заголовок: {article.title}\n"
            f"Лид: {article.lead}\n"
            f"Тематика: Технологии, инновации и управление клиентским опытом\n"
            f"Файл материала: {export_file}\n"
            f"Форма подачи: https://rb.ru/blogers/#rec764023212 или opinion@rb.ru\n"
            f"Регламент: Рассмотрение ~7 рабочих дней, план на 2-3 недели\n"
            f"--------------------------------------------------------------------"
        )
        pitch_file = self.exports_dir / f"pitch_rb_{article.id[:8]}.txt"
        with open(pitch_file, "w", encoding="utf-8") as f:
            f.write(pitch_text)

        return PublishResult(
            success=True,
            platform="rb",
            article_id=article.id,
            status="submitted",
            message=f"Сформирован пакет колумниста RB.RU. Питч сохранен: {pitch_file}"
        )
