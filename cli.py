#!/usr/bin/env python3
"""
CLI Controller for SERM Content Distribution, Business Lead Magnets & OPP Telegram Bot.
"""
import sys
import os
import re
import json
import inspect
import getpass
import argparse
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.models import Campaign, Article
from core.db import Database
from core.indexer import FastIndexer
from core.telegram_posts import TelegramPostsGenerator
from core import content, envfile
from publishers import registry
from config.settings import (PLATFORMS_CONFIG, EXPORTS_DIR, DATA_DIR, SESSIONS_DIR, ENV_FILE,
                             SettingsError, load_settings, get_bot_token, render_env_example)

TG_API = "https://api.telegram.org"
# Platforms with a browser session (data/sessions/<key>_profile): login / push-sessions / doctor
SESSION_PLATFORMS = [key for key, spec in registry.PLATFORMS.items() if spec.has_session]
# Remote paths go to ssh/scp unquoted, so only plain path characters are accepted
SAFE_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
# push-sessions: server-side backups of a session file kept (each is full access to the account)
SESSION_BACKUPS_KEPT = 5
# init: built-in content templates copied into profiles/<slug>/templates (the owner's texts from then on);
# admin/ and prompts/ are not copied: they come with engine updates
INIT_TEMPLATE_DIRS = ("articles", "bot", "telegram", "lead_magnet", "partials", "analytics", "pitch", "radar")
# Files the container runtime creates: doctor runs inside docker compose
CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def get_publisher(platform: str):
    return registry.get_publisher(platform)


def _load_profile(required: bool = True):
    """Profile by SERM_PROFILE / the single own profiles/*.yaml. Not required -> None with a warning."""
    from core.profile import ProfileError, get_profile
    try:
        return get_profile()
    except ProfileError as e:
        if required:
            raise SystemExit(f"❌ Профиль: {e}")
        print(f"⚠️  Профиль не найден, генерация в режиме template без профиля: {e}", file=sys.stderr)
        return None


def _ask_yes(question: str) -> bool:
    return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes", "д", "да")

def cmd_create_campaign(args, db: Database):
    campaign_id = str(uuid.uuid4())[:8]
    campaign = Campaign(
        id=campaign_id,
        brand_name=args.brand,
        target_query=args.query,
        seed_topic=args.seed
    )
    db.add_campaign(campaign)
    print(f"\n✅ Кампания успешно создана!")
    print(f"   ID кампании: {campaign_id}")
    print(f"   Бренд:       {campaign.brand_name}")
    print(f"   SERM-запрос: {campaign.target_query}")
    print(f"   Инфоповод:   {campaign.seed_topic}")
    print(f"\nСледующий шаг: запустите автопилот или генерацию статей:")
    print(f"   python3 cli.py autopilot --campaign {campaign_id}")
    print(f"   python3 cli.py generate --campaign {campaign_id}\n")

def _generation_options(args) -> dict:
    """--mode/--regenerate/--refresh-cache/--dry-run that were actually given."""
    options = {}
    if getattr(args, "mode", None):
        options["mode"] = args.mode
    for name in ("regenerate", "refresh_cache", "dry_run"):
        if getattr(args, name, False):
            options[name] = True
    return options


def _call_content(func, *call_args, **options):
    """Calls a core/content.py facade function; an option it does not accept is an error, not ignored."""
    params = inspect.signature(func).parameters
    unsupported = [name for name in options if name not in params]
    if unsupported:
        flags = ", ".join("--" + name.replace("_", "-") for name in unsupported)
        raise SystemExit(f"❌ {flags}: не поддерживается core/content.py.{func.__name__}() в этой версии.")
    return func(*call_args, **options)


def cmd_generate(args, db: Database):
    campaign = db.get_campaign(args.campaign)
    if not campaign:
        print(f"❌ Ошибка: Кампания '{args.campaign}' не найдена.")
        return

    options = _generation_options(args)
    profile = _load_profile(required=bool(options))
    if options.get("mode") == content.generation_mode(profile):
        options.pop("mode")  # already the profile's mode, nothing to override

    print(f"\n🔄 Генерация 7 уникальных редакционных адаптаций для кампании '{campaign.brand_name}'...")
    articles = _call_content(content.articles, campaign, profile, **options)

    if options.get("dry_run"):
        print("\n🧪 Dry-run: API не вызывался, в базу ничего не записано.\n")
        return

    for art in articles:
        db.add_article(art)
        plat_title = PLATFORMS_CONFIG.get(art.platform, {}).get("name", art.platform)
        print(f"   [+] {plat_title:<25} | Заголовок: {art.title[:45]}...")

    print(f"\n✅ Успешно сгенерировано {len(articles)} уникальных статей (100% защита от склейки дубликатов).")
    print(f"Посмотреть список: python3 cli.py list-articles --campaign {campaign.id}")
    print(f"Запустить публикацию: python3 cli.py publish --campaign {campaign.id}\n")

def cmd_generate_lead_magnet(args, db: Database):
    camp_dir = EXPORTS_DIR / f"campaign_{args.campaign}"
    camp_dir.mkdir(parents=True, exist_ok=True)
    topic = getattr(args, "topic", None) or "general_audit"
    print(f"\n📄 Генерация бизнес-лид-магнита (Markdown + Executive PDF) для темы: '{topic}'...")
    from core.lead_magnet import DynamicLeadMagnetGenerator
    lm_gen = DynamicLeadMagnetGenerator(camp_dir)
    res = asyncio.run(lm_gen.generate_for_topic(topic))
    print(f"✅ Лид-магнит создан:")
    print(f"   Название:    {res['title']}")
    print(f"   [MD]         {res['markdown']}")
    print(f"   [PDF]        {res['pdf']} ({res['size_bytes']:,} байт)")

def cmd_generate_tg_posts(args, db: Database):
    campaign = db.get_campaign(args.campaign)
    articles = db.list_articles_by_campaign(args.campaign)
    camp_dir = EXPORTS_DIR / f"campaign_{args.campaign}"
    camp_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n📱 Генерация пакета из 4 прогревающих постов для Telegram-канала...")
    tg_gen = TelegramPostsGenerator(camp_dir)
    post_file = tg_gen.generate_posts_pack(campaign, articles)
    print(f"✅ Пакет постов готов: {post_file}\n")

def cmd_autopilot(args, db: Database):
    campaign = db.get_campaign(args.campaign)
    if not campaign:
        print(f"❌ Ошибка: Кампания '{args.campaign}' не найдена.")
        return

    print(f"\n🚀 ЗАПУСК ПОЛНОГО АВТОПИЛОТА SERM ДЛЯ: {campaign.brand_name}")
    print("=" * 70)
    
    # 1. Generate 7 platform articles
    cmd_generate(args, db)

    # 2. Generate Lead Magnet (MD + PDF)
    cmd_generate_lead_magnet(args, db)

    # 3. Generate Telegram Channel Posts Pack
    cmd_generate_tg_posts(args, db)

    # 4. Export all materials
    cmd_export(args, db)

    # 5. Optional publish
    if args.publish:
        cmd_publish(args, db)

    print("\n🏁 АВТОПИЛОТ УСПЕШНО ЗАВЕРШЕН!")
    print(f"Все материалы собраны в: {EXPORTS_DIR / f'campaign_{campaign.id}'}\n")

def cmd_list_campaigns(args, db: Database):
    campaigns = db.list_campaigns()
    if not campaigns:
        print("Список кампаний пуст. Создайте новую: python3 cli.py create-campaign --brand ... --query ... --seed ...")
        return

    print("\n📋 Список SERM-кампаний:")
    print("-" * 80)
    print(f"{'ID':<10} | {'Бренд':<20} | {'SERM-запрос':<25} | {'Создана':<20}")
    print("-" * 80)
    for c in campaigns:
        print(f"{c.id:<10} | {c.brand_name:<20} | {c.target_query:<25} | {c.created_at[:19]:<20}")
    print("-" * 80)

def cmd_list_articles(args, db: Database):
    articles = db.list_articles_by_campaign(args.campaign)
    if not articles:
        print(f"Статьи для кампании '{args.campaign}' не найдены. Сгенерируйте их: python3 cli.py generate --campaign {args.campaign}")
        return

    print(f"\n📄 Статьи кампании {args.campaign}:")
    print("-" * 95)
    print(f"{'ID':<10} | {'Площадка':<15} | {'Статус':<15} | {'Заголовок':<45}")
    print("-" * 95)
    for a in articles:
        plat = PLATFORMS_CONFIG.get(a.platform, {}).get("name", a.platform)
        print(f"{a.id[:8]:<10} | {plat:<15} | {a.status:<15} | {a.title[:45]:<45}")
    print("-" * 95)

def cmd_publish(args, db: Database):
    articles = db.list_articles_by_campaign(args.campaign)
    if not articles:
        print(f"Нет статей для публикации в кампании '{args.campaign}'.")
        return

    platforms_filter = [p.strip().lower() for p in args.platforms.split(",")] if args.platforms else None

    print(f"\n🚀 Запуск пайплайна дистрибуции для кампании {args.campaign}...")
    indexer = FastIndexer()

    for article in articles:
        if platforms_filter and article.platform not in platforms_filter:
            continue

        plat_meta = PLATFORMS_CONFIG.get(article.platform, {})
        plat_name = plat_meta.get("name", article.platform)
        print(f"\n--- [{plat_name}] Обработка ---")

        publisher = get_publisher(article.platform)
        result = publisher.publish(article)

        if result.success:
            db.update_article_status(
                article_id=article.id,
                status=result.status,
                published_url=result.published_url
            )
            print(f"✅ Успех ({plat_name}): {result.message}")
            if result.published_url:
                print(f"   🔗 URL: {result.published_url}")
                if args.fast_index:
                    print("   ⚡ Отправка сигналов быстрой индексации (Telegram + VK + IndexNow)...")
                    idx_res = indexer.ping_all(article.id, article.title, result.published_url)
                    for ch, res in idx_res.items():
                        db.log_indexing(res)
                        status_sym = "✅" if res.success else "⚠️"
                        print(f"      {status_sym} {ch:<10}: {res.message}")
        else:
            db.update_article_status(
                article_id=article.id,
                status="failed",
                error_message=result.error
            )
            print(f"❌ Ошибка ({plat_name}): {result.error}")

    print("\n🏁 Пайплайн публикации завершен.")

def cmd_fast_index(args, db: Database):
    indexer = FastIndexer()
    print(f"\n⚡ Запуск быстрой индексации для URL: {args.url}")
    results = indexer.ping_all("manual", args.title or "Новый материал", args.url)
    for ch, res in results.items():
        status_sym = "✅" if res.success else "⚠️"
        print(f"   {status_sym} {ch:<10}: {res.message}")

def cmd_login(args):
    plat = (args.platform_name or args.platform or "").lower()
    if plat not in SESSION_PLATFORMS:
        print(f"❌ Ошибка: укажите площадку: {', '.join(SESSION_PLATFORMS)}")
        return 2
    publisher = get_publisher(plat)
    publisher.interactive_login()
    # vc/spark/executive log in into a persistent browser profile; the server needs storage_state.json
    from core.session_manager import SessionManager
    storage_file = asyncio.run(SessionManager.export_storage_state(plat))
    if storage_file:
        print(f"✅ Cookies сохранены в {storage_file}")
    print(f"Отправить сессию на сервер: python3 cli.py push-sessions {plat}")
    return 0

def cmd_export(args, db: Database):
    articles = db.list_articles_by_campaign(args.campaign)
    if not articles:
        print("Статьи не найдены.")
        return

    camp_dir = EXPORTS_DIR / f"campaign_{args.campaign}"
    camp_dir.mkdir(parents=True, exist_ok=True)

    for a in articles:
        file_path = camp_dir / f"{a.platform}_{a.title[:30].replace(' ', '_')}.md"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(a.content_markdown)

    print(f"\n📦 Все 7 адаптированных статей экспортированы в папку:")
    print(f"   {camp_dir}\n")

def cmd_run_opp_bot(args):
    from core.opp_bot import run_opp_bot
    bot_token = args.token or get_bot_token()
    if not bot_token:
        print("❌ Ошибка: Укажите токен бота через --token или переменную окружения OPP_BOT_TOKEN.")
        return
    settings = load_settings()
    profile = None
    if not (args.channel or settings.channel) or not (args.contact or settings.contact):
        profile = _load_profile()
    channel = args.channel or settings.channel or profile.telegram.channel
    pdf = Path(args.pdf) if args.pdf else (EXPORTS_DIR / f"campaign_{args.campaign}" / "lead_magnet_15_business_holes.pdf" if args.campaign else None)
    contact = args.contact or settings.contact or profile.telegram.contact
    print(f"🚀 Запуск ОПП-бота для канала {channel}...")
    print(f"   Лид-магнит: {pdf}")
    asyncio.run(run_opp_bot(bot_token, channel, pdf, contact))

def cmd_horizon(args, db: Database):
    from core.horizon_connector import HorizonConnector
    print(f"\n📡 Сканирование бизнес-инфоповодов через Horizon News Radar...")
    news = HorizonConnector.get_latest_news(force_refresh=args.refresh)
    print(f"✅ Найдено {len(news)} актуальных тем. Топ инфоповодов по шкале влияния на бизнес:")
    print("=" * 80)
    for i, item in enumerate(news[:args.limit], 1):
        score = item.get("business_score", 5.0)
        source = item.get("source_name", "СМИ")
        print(f"[{i:02d}] ⚡ Бизнес-скор: {score}/10 | Источник: {source}")
        print(f"     Заголовок: {item.get('title')}")
        print(f"     Ссылка:    {item.get('link')}")
        print(f"     Контекст:  {item.get('description')[:140]}...\n")

    if args.synthesize:
        idx = int(args.synthesize) - 1
        if 0 <= idx < len(news):
            selected = news[idx]
            print(f"⚡ Автоматический синтез кампании по новости #{args.synthesize}: '{selected.get('title')}'...")
            profile = _load_profile()
            res = HorizonConnector.prepare_campaign_from_news(selected, profile=profile)

            async def lead_magnet():
                # start_lead_magnet needs a running loop: the CLI has none until asyncio.run
                await HorizonConnector.start_lead_magnet(res, profile)

            try:
                asyncio.run(lead_magnet())
            except Exception as e:
                print(f"⚠️  Лид-магнит не создан: {e}")
            print(f"✅ Кампания успешно создана!")
            print(f"   ID:          {res['campaign_id']}")
            print(f"   Статей:      {res['articles_count']}")
            print(f"   Лид-магнит:  {res['lead_magnet_preset']}")
            print(f"   Папка:       {res['exports_dir']}")
            print(f"\nЗапустите публикацию: python3 cli.py publish --campaign {res['campaign_id']}\n")
        else:
            print("❌ Ошибка: Неверный номер новости.")

def _deploy_target(args):
    """(user@host, key path, install dir) from flags, else SERM_DEPLOY_HOST/KEY/DIR."""
    settings = load_settings()
    host = getattr(args, "host", None) or settings.deploy_host
    key = getattr(args, "key", None) or settings.deploy_key
    remote_dir = getattr(args, "remote_dir", None) or settings.deploy_dir
    missing = [name for name, value in (("--host / SERM_DEPLOY_HOST", host), ("--key / SERM_DEPLOY_KEY", key),
                                        ("--remote-dir / SERM_DEPLOY_DIR", remote_dir)) if not value]
    if missing:
        raise SystemExit(f"❌ Не задан адрес сервера: {', '.join(missing)}")
    if "@" not in host:
        host = f"root@{host}"  # historical default user of sync-sessions
    key = os.path.expanduser(key)
    if not Path(key).is_file():
        raise SystemExit(f"❌ SSH-ключ не найден: {key}")
    if not SAFE_REMOTE_PATH_RE.match(remote_dir):
        raise SystemExit(f"❌ SERM_DEPLOY_DIR: допустимы только латиница, цифры и ._-/ ({remote_dir!r})")
    return host, key, remote_dir.rstrip("/")


def cmd_push_sessions(args):
    """Delivers data/sessions/<platform>_profile/storage_state.json to the server, backing up its copy."""
    if args.all:
        platforms = [p for p in SESSION_PLATFORMS if (SESSIONS_DIR / f"{p}_profile" / "storage_state.json").is_file()]
    elif args.platform in SESSION_PLATFORMS:
        platforms = [args.platform]
    else:
        print(f"❌ Ошибка: укажите площадку ({', '.join(SESSION_PLATFORMS)}) или --all")
        return 2
    files = {p: SESSIONS_DIR / f"{p}_profile" / "storage_state.json" for p in platforms}
    absent = [p for p, f in files.items() if not f.is_file()]
    if absent or not files:
        print(f"❌ Нет локальной сессии: {', '.join(absent) or 'ни одной площадки'}. Сначала: python3 cli.py login <площадка>")
        return 1

    host, key, remote_dir = _deploy_target(args)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"\n🔐 Отправка cookie-сессий на {host} (cookies = полный доступ к аккаунтам):")
    for plat, local in files.items():
        modified = datetime.fromtimestamp(local.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"   {plat:<10} {local} ({local.stat().st_size} байт, {modified})")
        print(f"   {'':<10} → {remote_dir}/data/sessions/{plat}_profile/storage_state.json")
    print(f"   Серверная копия сохранится как storage_state.json.bak-{stamp} (хранятся {SESSION_BACKUPS_KEPT} последних)")
    if not args.yes and not _ask_yes("Заменить сессии на сервере?"):
        print("Отменено.")
        return 1

    ssh = ["ssh", "-i", key, host]
    for plat, local in files.items():
        target_dir = f"{remote_dir}/data/sessions/{plat}_profile"
        target = f"{target_dir}/storage_state.json"
        tmp = f"{target}.tmp-{stamp}"
        steps = [
            ssh + [f"mkdir -p {target_dir} && if [ -f {target} ]; then cp -p {target} {target}.bak-{stamp}; fi && "
                   f"for f in $(ls -1 {target}.bak-* 2>/dev/null | sort -r | tail -n +{SESSION_BACKUPS_KEPT + 1}); "
                   f"do rm -f \"$f\"; done"],
            ["scp", "-i", key, str(local), f"{host}:{tmp}"],
            ssh + [f"chmod 600 {tmp} && mv {tmp} {target}"],
        ]
        try:
            for step in steps:
                subprocess.run(step, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"❌ {plat}: ошибка доставки ({e}). Серверная сессия не заменена.")
            return 1
        print(f"✅ {plat}: сессия доставлена на {host}:{target}")
    return 0


def cmd_sync_sessions(args):
    """Legacy name: same as push-sessions --all."""
    args.all, args.platform = True, None
    return cmd_push_sessions(args)


def cmd_env_example(args):
    print(render_env_example(), end="")
    return 0


def _tg_api(token: str, method: str, **params):
    """Bot API call; the token goes only into the URL and never into messages."""
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(f"{TG_API}/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except ValueError:
            body = {"description": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"нет связи с Telegram API: {getattr(e, 'reason', e)}") from None
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or "ошибка Telegram API")
    return body["result"]


def _in_container() -> bool:
    return any(marker.exists() for marker in CONTAINER_MARKERS)


def cmd_doctor(args):
    """OK/WARN/FAIL table of the installation; exit code 1 on any FAIL. --offline: no Telegram API calls."""
    from core import templating
    from core.profile import ProfileError, load_profile, missing_own_templates, resolve_profile_path
    rows = []

    def add(status, check, detail=""):
        rows.append((status, check, detail))

    # Settings
    try:
        settings = load_settings(strict=True)
        add("OK", "Настройки", "формат значений корректен")
    except SettingsError as e:
        add("FAIL", "Настройки", str(e).replace("\n  ", "; "))
        try:
            settings = load_settings()
        except SettingsError:
            settings = None
    if settings:
        problems = set(settings.problems())
        for warning in settings.warnings():
            if warning not in problems:
                add("WARN", "Настройки", warning)

    # Profile
    profile = None
    try:
        profile = load_profile(resolve_profile_path())
        add("OK", "Профиль", f"{profile.slug} ({profile.source_path})")
        if profile.is_example:
            add("WARN", "Профиль", "выбран пример — бот и статьи будут от имени вымышленного бренда")
        else:
            # the daemon refuses to start without them (scheduler.main), so this is a FAIL, not a WARN
            problem = missing_own_templates(profile)
            if problem:
                add("FAIL", "Шаблоны профиля", problem)
            else:
                add("OK", "Шаблоны профиля", str(profile.templates_path))
    except ProfileError as e:
        add("FAIL", "Профиль", str(e).replace("\n", " "))

    # Templates (syntax only: each module renders with its own context)
    if profile:
        env = templating.get_environment(profile)
        names = env.list_templates()
        broken = []
        for name in names:
            try:
                env.get_template(name)
            except Exception as e:
                broken.append(f"{name}: {e}")
        if broken:
            add("FAIL", "Шаблоны", "; ".join(broken))
        else:
            add("OK", "Шаблоны", f"{len(names)} компилируются")
        add("OK", "Расписание", profile.schedule.summary())

    # Telegram bot and channel
    token = settings.bot_token.get_secret_value() if settings else ""
    channel = (settings.channel if settings else None) or (profile.telegram.channel if profile else None)
    if not token:
        add("WARN", "Бот", "OPP_BOT_TOKEN не задан — бот не запустится")
    elif args.offline:
        add("WARN", "Бот", "проверка бота и канала в Telegram пропущена (--offline)")
    else:
        try:
            me = _tg_api(token, "getMe")
            expected = profile.telegram.bot_username if profile else None
            if expected and expected.lower() != me.get("username", "").lower():
                add("FAIL", "Бот", f"токен от @{me.get('username')}, в профиле bot_username={expected}")
            else:
                add("OK", "Бот", f"@{me.get('username')}")
            if channel:
                member = _tg_api(token, "getChatMember", chat_id=channel, user_id=me["id"])
                if member.get("status") in ("administrator", "creator"):
                    add("OK", "Канал", f"бот — админ {channel}")
                else:
                    add("FAIL", "Канал", f"бот не админ {channel}: проверка подписки не работает")
            if args.send_test:
                if settings.admin_tg_id is None:
                    add("WARN", "Алерт", "ADMIN_TG_ID не задан")
                else:
                    _tg_api(token, "sendMessage", chat_id=settings.admin_tg_id, text="SERM doctor: test alert")
                    add("OK", "Алерт", f"тест-сообщение отправлено {settings.admin_tg_id}")
        except RuntimeError as e:
            add("FAIL", "Бот", str(e))

    # LLM key: needed only for generation.mode=llm
    try:
        mode = content.generation_mode(profile)
    except ValueError as e:
        mode = None
        add("FAIL", "Генерация", str(e))
    if mode == content.MODE_LLM:
        if settings and settings.llm_api_key.get_secret_value():
            add("OK", "Генерация", f"mode=llm, ключ задан, модель {settings.llm_model}")
        else:
            add("FAIL", "Генерация", "mode=llm, а POLZA_API_KEY / LLM_API_KEY не задан")
        if settings and settings.llm_max_cost_per_run is not None and (
                settings.llm_price_in is None or settings.llm_price_out is None):
            add("WARN", "Бюджет LLM", f"LLM_MAX_COST_PER_RUN={settings.llm_max_cost_per_run} применяется, только если "
                                      f"провайдер возвращает usage.cost; иначе задайте LLM_PRICE_INPUT_PER_MTOK и "
                                      f"LLM_PRICE_OUTPUT_PER_MTOK (или LLM_MAX_COST_PER_RUN=none — без лимита)")
    elif mode:
        add("OK", "Генерация", f"mode={mode}, ключ LLM не нужен")

    # Platform sessions
    if profile:
        from core.session_manager import SessionManager
        for plat in registry.enabled(profile):
            if plat not in SESSION_PLATFORMS:
                continue
            storage_file = SESSIONS_DIR / f"{plat}_profile" / "storage_state.json"
            if not storage_file.is_file():
                add("WARN", f"Сессия {plat}", f"нет {storage_file}: python3 cli.py login {plat}")
            elif args.sessions:
                res = asyncio.run(SessionManager.verify_session(plat))
                add("OK" if res.get("authorized") else "FAIL", f"Сессия {plat}", res.get("message", ""))
            else:
                modified = datetime.fromtimestamp(storage_file.stat().st_mtime).strftime("%Y-%m-%d")
                add("OK", f"Сессия {plat}", f"файл от {modified} (живость: --sessions)")

    # Write access
    add("OK" if os.access(DATA_DIR, os.W_OK) else "FAIL", "Данные", f"запись в {DATA_DIR}")
    env_target = ENV_FILE if ENV_FILE.exists() else ENV_FILE.parent
    add("OK" if os.access(env_target, os.W_OK) else "WARN", "Файл окружения", f"запись в {ENV_FILE}")

    # docker compose publishes the admin panel on ${ADMIN_BIND:-127.0.0.1}: the cookie-sync extension
    # cannot reach it from outside unless ADMIN_BIND is set (it is not a Settings field: compose reads it)
    if _in_container() and not (os.environ.get("ADMIN_BIND") or envfile.read(ENV_FILE).get("ADMIN_BIND")):
        add("WARN", "Админка", "ADMIN_BIND не задан: порт админки открыт только на 127.0.0.1 сервера — "
                               "расширение синхронизации cookie не достучится (если оно нужно, задайте ADMIN_BIND, "
                               "см. docker-compose.yml)")

    width = max(len(check) for _, check, _ in rows)
    for status, check, detail in rows:
        print(f"{status:<5} {check:<{width}}  {detail}")
    failed = sum(1 for status, _, _ in rows if status == "FAIL")
    print(f"\n{'❌' if failed else '✅'} FAIL: {failed}, WARN: {sum(1 for r in rows if r[0] == 'WARN')}")
    return 1 if failed else 0


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip() or default
        if answer:
            return answer


def _init_answers(args, example: dict) -> dict:
    """Wizard answers: stdin questions, or flags with --from-example (no secrets non-interactively)."""
    example_platforms = ",".join(k for k, v in (example.get("platforms") or {}).items() if v.get("enabled", True))
    if args.from_example:
        if not (args.name and args.channel and args.contact):
            raise SystemExit("❌ С --from-example нужны --name, --channel и --contact")
        return {
            "kind": args.from_example, "name": args.name, "display_name": args.display_name or args.name,
            "channel": args.channel, "contact": args.contact,
            "platforms": args.platforms or example_platforms,
            "slug": args.slug or _slugify(args.name), "bot_token": "", "llm_key": "",
        }
    kind = ""
    while kind not in ("person", "company"):
        kind = _ask("Кто говорит: person (личный бренд) или company", "person").lower()
    name = _ask("Имя (person: ФИО; company: название)")
    answers = {
        "kind": kind, "name": name,
        "display_name": _ask("Подпись в текстах", name),
        "channel": _ask("Канал Telegram (@name или -100…)"),
        "contact": _ask("Контакт для связи (@username)"),
        "platforms": _ask("Площадки через запятую", example_platforms),
        "slug": _ask("Имя файла профиля (латиница)", _slugify(name) or "brand"),
    }
    answers["bot_token"] = getpass.getpass("Токен бота от @BotFather (Enter — пропустить): ").strip()
    answers["llm_key"] = getpass.getpass("Ключ LLM-провайдера (Enter — пропустить): ").strip()
    return answers


def cmd_init(args):
    """Creates profiles/<slug>.yaml from an example and stores secrets in the .env file."""
    import yaml
    from pydantic import ValidationError
    from core.profile import EXAMPLE_PREFIX, PROFILES_DIR, Profile

    profiles_dir = Path(args.profiles_dir) if args.profiles_dir else PROFILES_DIR
    env_path = Path(args.env_file) if args.env_file else ENV_FILE
    interactive = not args.from_example

    def read_example(kind):
        return yaml.safe_load((PROFILES_DIR / f"{EXAMPLE_PREFIX}-{kind}.yaml").read_text(encoding="utf-8"))

    # platform defaults are offered from the person example; both examples are built on the same skeleton
    answers = _init_answers(args, read_example(args.from_example or "person"))
    example = read_example(answers["kind"])

    slug = answers["slug"]
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", slug) or slug.startswith(EXAMPLE_PREFIX):
        print(f"❌ Имя профиля {slug!r}: латиница/цифры/-/_, не начинается с '{EXAMPLE_PREFIX}'")
        return 1

    # Identity comes from the answers; offer, lead magnets and voice stay from the example to edit by hand
    enabled = {p.strip() for p in answers["platforms"].split(",") if p.strip()}
    unknown = enabled - set(registry.PLATFORMS)
    if unknown:
        print(f"❌ Неизвестные площадки: {', '.join(sorted(unknown))}. Есть: {', '.join(registry.PLATFORMS)}")
        return 1
    data = dict(example)
    data.update({
        "kind": answers["kind"], "name": answers["name"], "display_name": answers["display_name"], "aliases": [],
        "seo": {"target_query": f"{answers['name']} отзывы", "seed_topic": ""},
        "campaign_id": uuid.uuid4().hex[:8],
        "platforms": {k: {**(example.get("platforms") or {}).get(k, {}), "enabled": k in enabled}
                      for k in registry.PLATFORMS},
        "deploy": {"server_url": None},
    })
    data["telegram"] = {**(example.get("telegram") or {}), "channel": answers["channel"],
                        "contact": answers["contact"], "channel_title": "", "bot_username": None}

    env_updates = {"SERM_PROFILE": slug}
    if answers["bot_token"]:
        try:
            me = _tg_api(answers["bot_token"], "getMe")
            data["telegram"]["bot_username"] = me.get("username")
            print(f"✅ Токен рабочий: @{me.get('username')}")
            env_updates["OPP_BOT_TOKEN"] = answers["bot_token"]
        except RuntimeError as e:
            print(f"⚠️  getMe не прошёл: {e}")
            if _ask_yes("Сохранить токен всё равно?"):
                env_updates["OPP_BOT_TOKEN"] = answers["bot_token"]
    if answers["llm_key"]:
        env_updates["POLZA_API_KEY"] = answers["llm_key"]

    try:
        Profile.model_validate(data)
    except ValidationError as e:
        print("❌ Профиль не прошёл проверку, ничего не записано:")
        for err in e.errors():
            print(f"   {'.'.join(str(part) for part in err['loc'])}: {err['msg']}")
        return 1

    profile_path = profiles_dir / f"{slug}.yaml"
    if profile_path.exists():
        allowed = _ask_yes(f"{profile_path} уже есть. Перезаписать?") if interactive else args.force
        if not allowed:
            print(f"❌ {profile_path} уже есть — не перезаписан (неинтерактивно: --force).")
            return 1
    profiles_dir.mkdir(parents=True, exist_ok=True)
    header = (f"# Профиль создан `cli.py init` {datetime.now():%Y-%m-%d} из {EXAMPLE_PREFIX}-{answers['kind']}.yaml.\n"
              "# Отредактируйте voice, offer_tiers и lead_magnets под себя. Схема: core/profile.py.\n")
    profile_path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"✅ Профиль: {profile_path}")
    _init_templates(profiles_dir / slug / "templates", interactive, args.force)

    # .env: existing different values are replaced only after confirmation; values are never printed
    from core import envfile
    current = envfile.read(env_path)
    for key in list(env_updates):
        if current.get(key) and current[key] != env_updates[key]:
            allowed = _ask_yes(f"В {env_path.name} уже задан {key}. Заменить?") if interactive else args.force
            if not allowed:
                print(f"   {key}: оставлено прежнее значение")
                env_updates.pop(key)
    existed = env_path.exists()
    envfile.write(env_path, env_updates)
    if not existed:
        os.chmod(env_path, 0o600)
    print(f"✅ {env_path}: {', '.join(env_updates) or 'без изменений'}")
    print(f"\nОтредактируйте тексты в profiles/{slug}/templates/ — это ваши тексты.")
    print("Дальше: python3 cli.py doctor → python3 cli.py login <площадка> → python3 cli.py push-sessions <площадка>")
    return 0


def _init_templates(target: Path, interactive: bool, force: bool) -> None:
    """Copies INIT_TEMPLATE_DIRS of the built-in templates to the profile; an existing dir only after confirmation."""
    from core.templating import BUILTIN_TEMPLATES_DIR
    if target.exists():
        allowed = _ask_yes(f"{target} уже есть. Перезаписать встроенными шаблонами?") if interactive else force
        if not allowed:
            print(f"   {target}: оставлен как есть")
            return
    target.mkdir(parents=True, exist_ok=True)
    for name in INIT_TEMPLATE_DIRS:
        source = BUILTIN_TEMPLATES_DIR / name
        if source.is_dir():
            shutil.copytree(source, target / name, dirs_exist_ok=True)
    print(f"✅ Шаблоны: {target}")


def _usage_tokens(record: dict) -> int:
    # Contract 7 names the field `tokens` without a shape: accept a number, a dict or flat *_tokens keys
    tokens = record.get("tokens")
    if isinstance(tokens, (int, float)):
        return int(tokens)
    source = tokens if isinstance(tokens, dict) else record
    for key in ("total", "total_tokens"):
        if source.get(key) is not None:
            return int(source[key])
    return sum(int(source.get(key) or 0) for key in ("prompt", "completion", "prompt_tokens", "completion_tokens"))


def cmd_llm_usage(args):
    """Summary of data/llm_usage.jsonl by campaign and model."""
    path = DATA_DIR / "llm_usage.jsonl"
    if not path.is_file():
        print(f"Расходов LLM ещё нет: {path} не найден.")
        return 0
    groups, skipped = {}, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if args.campaign and record.get("campaign_id") != args.campaign:
            continue
        g = groups.setdefault((record.get("campaign_id") or "-", record.get("model") or "-"),
                              {"calls": 0, "cached": 0, "tokens": 0, "cost": Decimal(0), "unpriced": 0})
        g["calls"] += 1
        g["cached"] += 1 if record.get("cached") else 0
        g["tokens"] += _usage_tokens(record)
        if record.get("cost") is None:
            g["unpriced"] += 0 if record.get("cached") else 1
        else:
            g["cost"] += Decimal(str(record["cost"]))

    print(f"\n💰 Расходы LLM ({path}):")
    print("-" * 90)
    print(f"{'Кампания':<10} | {'Модель':<32} | {'Вызовы':>6} | {'Кэш':>5} | {'Токены':>9} | {'Стоимость':>10}")
    print("-" * 90)
    total = Decimal(0)
    for (campaign, model), g in sorted(groups.items()):
        cost = f"{g['cost']:.4f}" + ("*" if g["unpriced"] else "")
        print(f"{campaign:<10} | {model[:32]:<32} | {g['calls']:>6} | {g['cached']:>5} | {g['tokens']:>9} | {cost:>10}")
        total += g["cost"]
    print("-" * 90)
    print(f"Итого: {total:.4f} (в валюте счёта провайдера)")
    if any(g["unpriced"] for g in groups.values()):
        print("* часть вызовов без цены: провайдер не вернул cost, а LLM_PRICE_*_PER_MTOK не заданы")
    if skipped:
        print(f"⚠️  Пропущено битых строк: {skipped}")
    return 0

def _add_generation_flags(p):
    p.add_argument("--mode", choices=[content.MODE_TEMPLATE, content.MODE_LLM], help="Перекрыть generation.mode профиля")
    p.add_argument("--regenerate", action="store_true", help="Создать статьи заново, даже если они уже есть")
    p.add_argument("--refresh-cache", action="store_true", help="Не брать ответы LLM из кэша")
    p.add_argument("--dry-run", action="store_true", help="Показать промпты и оценку стоимости без вызова API")


def _add_deploy_flags(p):
    p.add_argument("--host", help="Сервер: user@host (по умолчанию SERM_DEPLOY_HOST)")
    p.add_argument("--key", help="Путь к SSH-ключу (по умолчанию SERM_DEPLOY_KEY)")
    p.add_argument("--remote-dir", help="Каталог установки на сервере (по умолчанию SERM_DEPLOY_DIR)")
    p.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SERM Content Distribution, Lead Magnet & Fast Indexing CLI")
    subparsers = parser.add_subparsers(dest="command")

    # init
    p_init = subparsers.add_parser("init", help="Мастер настройки: профиль бренда + секреты в файле окружения")
    p_init.add_argument("--from-example", choices=["person", "company"], help="Без вопросов: взять пример и флаги")
    p_init.add_argument("--name", help="person: ФИО; company: название")
    p_init.add_argument("--display-name", help="Подпись в текстах (по умолчанию --name)")
    p_init.add_argument("--channel", help="Канал Telegram: @name или -100…")
    p_init.add_argument("--contact", help="Контакт: @username")
    p_init.add_argument("--platforms", help="Площадки через запятую")
    p_init.add_argument("--slug", help="Имя файла профиля (латиница)")
    p_init.add_argument("--force", action="store_true", help="Неинтерактивно: перезаписать профиль и ключи окружения")
    p_init.add_argument("--profiles-dir", help=argparse.SUPPRESS)
    p_init.add_argument("--env-file", help=argparse.SUPPRESS)

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Проверить установку: настройки, профиль, бот, канал, сессии")
    p_doc.add_argument("--sessions", action="store_true", help="Проверить живость сессий в браузере (медленно)")
    p_doc.add_argument("--send-test", action="store_true", help="Отправить тест-алерт на ADMIN_TG_ID")
    p_doc.add_argument("--offline", action="store_true",
                       help="Не обращаться к Telegram API (гейт деплоя: сбой сети не блокирует выкатку)")

    # env-example
    subparsers.add_parser("env-example", help="Напечатать пример файла окружения из схемы настроек")

    # push-sessions
    p_push = subparsers.add_parser("push-sessions", help="Отправить cookie-сессию площадки на сервер (с бэкапом)")
    p_push.add_argument("platform", nargs="?", choices=SESSION_PLATFORMS, help="Площадка")
    p_push.add_argument("--all", action="store_true", help="Все площадки с локальной сессией")
    _add_deploy_flags(p_push)

    # llm-usage
    p_usage = subparsers.add_parser("llm-usage", help="Сводка расходов LLM из data/llm_usage.jsonl")
    p_usage.add_argument("--campaign", help="Только эта кампания")

    # create-campaign
    p_create = subparsers.add_parser("create-campaign", help="Создать новую SERM кампанию")
    p_create.add_argument("--brand", required=True, help="Название бренда / персоны")
    p_create.add_argument("--query", required=True, help="Целевой поисковый запрос (например: 'Ромашка отзывы')")
    p_create.add_argument("--seed", required=True, help="Основной тезис или инфоповод")

    # generate
    p_gen = subparsers.add_parser("generate", help="Сгенерировать 7 адаптаций для кампании")
    p_gen.add_argument("--campaign", required=True, help="ID кампании")
    _add_generation_flags(p_gen)

    # generate-lead-magnet
    p_lm = subparsers.add_parser("generate-lead-magnet", help="Сгенерировать бизнес-лид-магнит (MD + PDF)")
    p_lm.add_argument("--campaign", required=True, help="ID кампании")
    p_lm.add_argument("--topic", default="general_audit", help="Тема или пресет (fiscal_tax, ltv_retention, dev_cost_reduction, general_audit или произвольный текст)")

    # horizon
    p_hz = subparsers.add_parser("horizon", help="Радар бизнес-инфоповодов Horizon (сканирование и синтез)")
    p_hz.add_argument("--refresh", action="store_true", help="Принудительно обновить RSS-источники")
    p_hz.add_argument("--limit", type=int, default=10, help="Количество выводимых инфоповодов")
    p_hz.add_argument("--synthesize", help="Номер новости для мгновенного создания кампании и лид-магнита")

    # sync-sessions
    p_ss = subparsers.add_parser("sync-sessions", help="Устарело: то же, что push-sessions --all")
    _add_deploy_flags(p_ss)

    # generate-tg-posts
    p_tg = subparsers.add_parser("generate-tg-posts", help="Сгенерировать пакет постов для Telegram-канала")
    p_tg.add_argument("--campaign", required=True, help="ID кампании")

    # autopilot
    p_auto = subparsers.add_parser("autopilot", help="Полный автопилот: статьи + лид-магнит + TG посты + экспорт")
    p_auto.add_argument("--campaign", required=True, help="ID кампании")
    p_auto.add_argument("--publish", action="store_true", default=False, help="Также запустить публикацию")
    p_auto.add_argument("--platforms", help="Список площадок через запятую")
    p_auto.add_argument("--fast-index", action="store_true", default=True, help="Пинговать быструю индексацию")
    _add_generation_flags(p_auto)

    # list-campaigns
    subparsers.add_parser("list-campaigns", help="Список всех кампаний")

    # list-articles
    p_la = subparsers.add_parser("list-articles", help="Список статей кампании")
    p_la.add_argument("--campaign", required=True, help="ID кампании")

    # publish
    p_pub = subparsers.add_parser("publish", help="Запустить публикацию/отправку статей")
    p_pub.add_argument("--campaign", required=True, help="ID кампании")
    p_pub.add_argument("--platforms", help="Список площадок через запятую (например: vc,spark,dzen)")
    p_pub.add_argument("--fast-index", action="store_true", default=True, help="Автоматически пинговать быструю индексацию")

    # fast-index
    p_idx = subparsers.add_parser("fast-index", help="Ручной пинг быстрой индексации для конкретной ссылки")
    p_idx.add_argument("--url", required=True, help="URL статьи")
    p_idx.add_argument("--title", default="", help="Заголовок статьи")

    # login
    p_log = subparsers.add_parser("login", help="Интерактивный вход в браузерный профиль площадки")
    p_log.add_argument("platform_name", nargs="?", choices=SESSION_PLATFORMS, help="Площадка для авторизации")
    p_log.add_argument("--platform", choices=SESSION_PLATFORMS, help="То же, прежняя форма")

    # export
    p_exp = subparsers.add_parser("export", help="Экспорт всех статей кампании в Markdown файлы")
    p_exp.add_argument("--campaign", required=True, help="ID кампании")

    # run-opp-bot
    p_bot = subparsers.add_parser("run-opp-bot", help="Запустить ОПП Telegram-бота (проверка подписки и выдача лид-магнита)")
    p_bot.add_argument("--token", help="Telegram Bot Token")
    p_bot.add_argument("--campaign", help="ID кампании для привязки PDF-файла")
    p_bot.add_argument("--channel", help="Канал для проверки подписки (по умолчанию OPP_CHANNEL / профиль)")
    p_bot.add_argument("--pdf", help="Прямой путь к PDF-файлу лид-магнита")
    p_bot.add_argument("--contact", help="Контакт для обращений (по умолчанию OPP_CONTACT / профиль)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return

    # Commands that need no database
    no_db = {"init": cmd_init, "doctor": cmd_doctor, "env-example": cmd_env_example,
             "push-sessions": cmd_push_sessions, "sync-sessions": cmd_sync_sessions,
             "llm-usage": cmd_llm_usage, "login": cmd_login}
    if args.command in no_db:
        sys.exit(no_db[args.command](args) or 0)

    db = Database()

    if args.command == "create-campaign":
        cmd_create_campaign(args, db)
    elif args.command == "generate":
        cmd_generate(args, db)
    elif args.command == "generate-lead-magnet":
        cmd_generate_lead_magnet(args, db)
    elif args.command == "generate-tg-posts":
        cmd_generate_tg_posts(args, db)
    elif args.command == "autopilot":
        cmd_autopilot(args, db)
    elif args.command == "list-campaigns":
        cmd_list_campaigns(args, db)
    elif args.command == "list-articles":
        cmd_list_articles(args, db)
    elif args.command == "publish":
        cmd_publish(args, db)
    elif args.command == "fast-index":
        cmd_fast_index(args, db)
    elif args.command == "export":
        cmd_export(args, db)
    elif args.command == "run-opp-bot":
        cmd_run_opp_bot(args)
    elif args.command == "horizon":
        cmd_horizon(args, db)

if __name__ == "__main__":
    main()
