"""
cli.py (Contract 5): old commands parse as before, push-sessions has no built-in server and backs
up the server copy, doctor/init never touch the network or the real .env (Telegram API is mocked,
.env is a temp file), extension carries no built-in server or token.
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
from config import settings  # noqa: E402
from core import envfile  # noqa: E402
from core.profile import get_profile, load_profile  # noqa: E402

TOKEN = "123456:" + "T" * 35
IPV4_RE = re.compile(r"\b(?!127\.0\.0\.1\b)\d{1,3}(?:\.\d{1,3}){3}\b")

ALL_ENV_NAMES = sorted({
    name for field in settings.Settings.model_fields for name in settings._env_names(field)
})

# Command lines of the CLI before Contract 5 — must keep parsing
LEGACY_ARGV = [
    ["create-campaign", "--brand", "B", "--query", "B отзывы", "--seed", "S"],
    ["generate", "--campaign", "c1"],
    ["generate-lead-magnet", "--campaign", "c1", "--topic", "fiscal_tax"],
    ["horizon", "--refresh", "--limit", "3", "--synthesize", "1"],
    ["sync-sessions", "--host", "srv.example.com", "--key", "/tmp/k"],
    ["generate-tg-posts", "--campaign", "c1"],
    ["autopilot", "--campaign", "c1", "--publish", "--platforms", "vc,spark"],
    ["list-campaigns"],
    ["list-articles", "--campaign", "c1"],
    ["publish", "--campaign", "c1", "--platforms", "vc"],
    ["fast-index", "--url", "https://example.com/a", "--title", "T"],
    ["login", "--platform", "vc"],
    ["export", "--campaign", "c1"],
    ["run-opp-bot", "--token", TOKEN, "--campaign", "c1", "--channel", "@chan_x", "--pdf", "x.pdf", "--contact", "@cont_x"],
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real .env, no env vars of the installation, no network."""
    for name in ALL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings.Settings, "env_path", env_file)
    monkeypatch.setattr(cli, "ENV_FILE", env_file)
    monkeypatch.setattr(cli, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)

    def no_network(*a, **kw):
        raise AssertionError("network call in a test")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    get_profile.cache_clear()
    yield env_file
    get_profile.cache_clear()


@pytest.fixture
def template_profile(monkeypatch, tmp_path):
    """example-person without the `generation` section (examples ship with mode: llm) -> template mode."""
    import yaml
    data = yaml.safe_load((PROJECT_ROOT / "profiles" / "example-person.yaml").read_text(encoding="utf-8"))
    data.pop("generation", None)
    path = tmp_path / "own-profiles" / "brand-person.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (path.parent / "brand-person" / "templates").mkdir(parents=True)  # own installation: override dir
    (path.parent / "brand-person" / "templates" / "own_marker.j2").write_text("{# own #}\n", encoding="utf-8")
    monkeypatch.setenv("SERM_PROFILE", str(path))
    get_profile.cache_clear()
    return path


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram Bot API: records calls, answers from `answers`."""
    calls = []
    answers = {"getMe": {"id": 42, "username": "test_brand_bot"},
               "getChatMember": {"status": "administrator"},
               "sendMessage": {"message_id": 1}}

    def fake(token, method, **params):
        calls.append((method, params))
        answer = answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cli, "_tg_api", fake)
    return SimpleNamespace(calls=calls, answers=answers)


# --- backward compatibility -------------------------------------------------

@pytest.mark.parametrize("argv", LEGACY_ARGV, ids=lambda a: a[0])
def test_legacy_command_lines_parse(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.command == argv[0]


def test_generate_without_flags_is_template_output(monkeypatch, template_profile):
    from core.generator import ContentGenerator
    campaign = SimpleNamespace(id="c1", brand_name="Тест Бренд", target_query="Тест Бренд отзывы", seed_topic="тема")
    added = []
    db = SimpleNamespace(get_campaign=lambda cid: campaign, add_article=added.append)
    cli.cmd_generate(cli.build_parser().parse_args(["generate", "--campaign", "c1"]), db)
    expected = ContentGenerator().generate_campaign_articles("c1", campaign.brand_name, campaign.target_query,
                                                              campaign.seed_topic)
    assert [(a.platform, a.title, a.content_markdown) for a in added] == \
           [(a.platform, a.title, a.content_markdown) for a in expected]


# --- generation flags go to core/content.py ---------------------------------

def _fake_articles(calls):
    def articles(campaign, profile=None, news=None, *, regenerate=False):
        calls.append({"regenerate": regenerate})
        return []
    return articles


def test_generation_flag_is_passed_to_facade(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.content, "articles", _fake_articles(calls))
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    db = SimpleNamespace(get_campaign=lambda cid: SimpleNamespace(id=cid, brand_name="B", target_query="q", seed_topic="s"),
                         add_article=lambda a: None)
    cli.cmd_generate(cli.build_parser().parse_args(["generate", "--campaign", "c1", "--regenerate"]), db)
    assert calls == [{"regenerate": True}]


def test_generation_flag_unknown_to_facade_is_an_error(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.content, "articles", _fake_articles(calls))
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    db = SimpleNamespace(get_campaign=lambda cid: SimpleNamespace(id=cid, brand_name="B", target_query="q", seed_topic="s"))
    with pytest.raises(SystemExit, match="--dry-run"):
        cli.cmd_generate(cli.build_parser().parse_args(["generate", "--campaign", "c1", "--dry-run"]), db)
    assert calls == []


# --- push-sessions ----------------------------------------------------------

@pytest.fixture
def deploy(monkeypatch, tmp_path, isolated):
    key = tmp_path / "id_test"
    key.write_text("not a key", encoding="utf-8")
    envfile.write(isolated, {"SERM_DEPLOY_HOST": "srv.example.com", "SERM_DEPLOY_KEY": str(key),
                             "SERM_DEPLOY_DIR": "/srv/serm"}, backup=False)
    session = tmp_path / "sessions" / "vc_profile" / "storage_state.json"
    session.parent.mkdir(parents=True)
    session.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    runs = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check: runs.append(cmd))
    return SimpleNamespace(key=str(key), session=session, runs=runs)


def test_push_sessions_backs_up_then_replaces(deploy, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.cmd_push_sessions(cli.build_parser().parse_args(["push-sessions", "vc"])) == 0
    backup, copy, swap = deploy.runs
    target = "/srv/serm/data/sessions/vc_profile/storage_state.json"
    assert backup[:4] == ["ssh", "-i", deploy.key, "root@srv.example.com"]
    assert f"cp -p {target} {target}.bak-" in backup[4]
    assert copy[0] == "scp" and copy[3] == str(deploy.session) and copy[4].startswith(f"root@srv.example.com:{target}.tmp-")
    assert f"mv {target}.tmp-" in swap[4] and swap[4].endswith(target)
    assert not any("StrictHostKeyChecking" in part for cmd in deploy.runs for part in cmd)


def test_push_sessions_keeps_last_server_backups(deploy, tmp_path):
    # CFG-L5: every backup is full access to the account; keep SESSION_BACKUPS_KEPT, drop older ones.
    # The remote command is run by a real shell against a local "server" directory.
    assert cli.cmd_push_sessions(cli.build_parser().parse_args(["push-sessions", "vc", "--yes"])) == 0
    remote = deploy.runs[0][4].replace("/srv/serm", str(tmp_path / "srv"))
    server_dir = tmp_path / "srv" / "data" / "sessions" / "vc_profile"
    server_dir.mkdir(parents=True)
    (server_dir / "storage_state.json").write_text("current", encoding="utf-8")
    for i in range(7):
        (server_dir / f"storage_state.json.bak-2020010{i}-000000").write_text("old", encoding="utf-8")
    subprocess.check_call(["bash", "-c", remote])  # subprocess.run is faked by the fixture
    backups = sorted(p.name for p in server_dir.glob("storage_state.json.bak-*"))
    assert len(backups) == cli.SESSION_BACKUPS_KEPT == 5
    assert "storage_state.json.bak-20200100-000000" not in backups  # oldest dropped
    assert (server_dir / backups[-1]).read_text(encoding="utf-8") == "current"  # newest = the copy just made


def test_horizon_synthesize_runs_lead_magnet_without_a_loop(monkeypatch, template_profile, tmp_path, capsys):
    # the CLI has no running loop: the lead magnet task used to fail with RuntimeError (no running event loop)
    from core import content
    from core.horizon_connector import HorizonConnector
    item = {"title": "Новость", "description": "Описание новости", "link": "https://news.example.org/1",
            "source_name": "RSS", "business_score": 7.0}
    res = {"campaign_id": "camp0001", "articles_count": 3, "lead_magnet_preset": "audit",
           "exports_dir": str(tmp_path / "exports")}
    made = []

    async def fake_lead_magnet(preset, profile, output_dir):
        made.append((preset, profile.slug, output_dir))

    monkeypatch.setattr(HorizonConnector, "get_latest_news", classmethod(lambda cls, force_refresh=False: [item]))
    monkeypatch.setattr(HorizonConnector, "prepare_campaign_from_news",
                        classmethod(lambda cls, news_item, *a, **kw: res))
    monkeypatch.setattr(content, "lead_magnet", fake_lead_magnet)
    cli.cmd_horizon(cli.build_parser().parse_args(["horizon", "--synthesize", "1"]), db=None)
    assert made == [("audit", "brand-person", tmp_path / "exports")]
    out = capsys.readouterr().out
    assert "camp0001" in out and "Лид-магнит не создан" not in out


def test_push_sessions_asks_before_touching_server(deploy, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.cmd_push_sessions(cli.build_parser().parse_args(["push-sessions", "--all"])) == 1
    assert deploy.runs == []


def test_sync_sessions_is_push_all(deploy):
    args = cli.build_parser().parse_args(["sync-sessions", "--yes", "--host", "deploy@other.example.com"])
    assert cli.cmd_sync_sessions(args) == 0
    assert deploy.runs[0][3] == "deploy@other.example.com"


def test_push_sessions_without_server_config_fails(tmp_path, monkeypatch):
    session = tmp_path / "sessions" / "vc_profile" / "storage_state.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("ssh without a configured server"))
    with pytest.raises(SystemExit, match="SERM_DEPLOY_HOST"):
        cli.cmd_push_sessions(cli.build_parser().parse_args(["push-sessions", "vc", "--yes"]))


def test_no_built_in_server_or_host_key_bypass():
    text = (PROJECT_ROOT / "cli.py").read_text(encoding="utf-8")
    assert not IPV4_RE.search(text)
    assert "StrictHostKeyChecking=no" not in text
    assert "~/.ssh/" not in text


# --- doctor -----------------------------------------------------------------

def _doctor(argv=()):
    return cli.cmd_doctor(cli.build_parser().parse_args(["doctor", *argv]))


def test_doctor_ok_on_template_profile(isolated, tg, template_profile, capsys):
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    assert _doctor() == 0
    out = capsys.readouterr().out
    assert str(template_profile) in out  # the chosen profile path is printed
    assert re.search(r"OK\s+Генерация\s+mode=template", out)
    assert "@test_brand_bot" in out and TOKEN not in out
    assert [c[0] for c in tg.calls] == ["getMe", "getChatMember"]
    assert tg.calls[1][1] == {"chat_id": "@example_channel", "user_id": 42}


def test_doctor_fails_when_bot_is_not_channel_admin(isolated, tg, template_profile, capsys):
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    tg.answers["getChatMember"] = {"status": "left"}
    assert _doctor() == 1
    assert "не админ" in capsys.readouterr().out


def test_doctor_llm_profile_needs_key(monkeypatch, isolated, tg, capsys):
    # shipped examples are mode: llm — without a key doctor must FAIL, with one it is OK (no LLM call made)
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    assert _doctor() == 1
    assert re.search(r"FAIL\s+Генерация\s+mode=llm", capsys.readouterr().out)
    envfile.write(isolated, {"LLM_API_KEY": "sk-test-key-0000"}, backup=False)
    assert _doctor() == 0
    out = capsys.readouterr().out
    assert re.search(r"OK\s+Генерация\s+mode=llm, ключ задан", out) and "sk-test-key-0000" not in out


def test_doctor_fails_on_bad_format_and_missing_profile(monkeypatch, isolated, tg, capsys):
    monkeypatch.setenv("SERM_PROFILE", "no-such-profile")
    envfile.write(isolated, {"OPP_CHANNEL": "not a channel"}, backup=False)
    assert _doctor() == 1
    out = capsys.readouterr().out
    assert re.search(r"FAIL\s+Настройки .*OPP_CHANNEL", out)
    assert re.search(r"FAIL\s+Профиль", out)
    assert tg.calls == []  # no token -> no API calls


def test_doctor_fails_without_own_templates(isolated, tg, template_profile, capsys):
    # CFG-H2: an own profile without profiles/<name>/templates would speak with the neutral scaffold;
    # the daemon refuses to start, so the deploy gate must stop here
    (template_profile.parent / "brand-person" / "templates" / "own_marker.j2").unlink()
    (template_profile.parent / "brand-person" / "templates").rmdir()
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    assert _doctor() == 1
    out = capsys.readouterr().out
    assert re.search(r"FAIL\s+Шаблоны профиля .*brand-person", out)


def test_doctor_fails_on_empty_own_templates_dir(isolated, tg, template_profile, capsys):
    # N5: an empty dir (partial rsync, mkdir "to make the deploy pass") is the same neutral scaffold
    (template_profile.parent / "brand-person" / "templates" / "own_marker.j2").unlink()
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    assert _doctor() == 1
    assert re.search(r"FAIL\s+Шаблоны профиля .*brand-person", capsys.readouterr().out)


def test_doctor_prints_own_templates_path(isolated, tg, template_profile, capsys):
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    assert _doctor() == 0
    templates = template_profile.parent / "brand-person" / "templates"
    assert re.search(r"OK\s+Шаблоны профиля\s+" + re.escape(str(templates.resolve())), capsys.readouterr().out)


def test_doctor_example_profile_has_no_templates_check(monkeypatch, isolated, tg, capsys):
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    _doctor()
    assert "Шаблоны профиля" not in capsys.readouterr().out


def test_doctor_offline_makes_no_telegram_calls(isolated, tg, template_profile, capsys):
    # CFG-M5: the deploy gate must not fail because Telegram is unreachable from the server
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    tg.answers["getMe"] = RuntimeError("нет связи с Telegram API: timed out")
    assert _doctor() == 1  # online: network failure is a FAIL
    capsys.readouterr()
    assert _doctor(["--offline"]) == 0
    assert re.search(r"WARN\s+Бот\s+.*--offline", capsys.readouterr().out)
    assert [c[0] for c in tg.calls] == ["getMe"]  # only the online run called the API


def test_doctor_warns_budget_cannot_be_applied(monkeypatch, isolated, tg, capsys):
    # CFG-M3: mode=llm, default budget, no LLM_PRICE_* -> the limit works only if the provider returns cost
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN, "LLM_API_KEY": "sk-test-key-0000"}, backup=False)
    _doctor()
    assert re.search(r"WARN\s+Бюджет LLM\s+LLM_MAX_COST_PER_RUN=300 .*LLM_PRICE_OUTPUT_PER_MTOK", capsys.readouterr().out)
    envfile.write(isolated, {"LLM_PRICE_INPUT_PER_MTOK": "3", "LLM_PRICE_OUTPUT_PER_MTOK": "15"}, backup=False)
    _doctor()
    assert "Бюджет LLM" not in capsys.readouterr().out
    envfile.write(isolated, {"LLM_PRICE_INPUT_PER_MTOK": "", "LLM_MAX_COST_PER_RUN": "none"}, backup=False)
    _doctor()
    assert "Бюджет LLM" not in capsys.readouterr().out


def test_doctor_warns_admin_bind_in_container(monkeypatch, isolated, tg, template_profile, tmp_path, capsys):
    # CFG-M4: compose publishes the admin on 127.0.0.1 unless ADMIN_BIND is set — the extension goes silent
    marker = tmp_path / "dockerenv"
    monkeypatch.setattr(cli, "CONTAINER_MARKERS", (marker,))
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN}, backup=False)
    _doctor()
    assert "ADMIN_BIND" not in capsys.readouterr().out  # not in a container: compose is not involved
    marker.write_text("", encoding="utf-8")
    _doctor()
    assert re.search(r"WARN\s+Админка\s+ADMIN_BIND не задан", capsys.readouterr().out)
    envfile.write(isolated, {"ADMIN_BIND": "127.0.0.1"}, backup=False)
    _doctor()
    assert "ADMIN_BIND не задан" not in capsys.readouterr().out
    monkeypatch.setenv("ADMIN_BIND", "127.0.0.1")
    envfile.write(isolated, {"ADMIN_BIND": ""}, backup=False)
    _doctor()
    assert "ADMIN_BIND не задан" not in capsys.readouterr().out  # compose env_file puts it into the process env


def test_doctor_send_test_alert(isolated, tg, template_profile):
    envfile.write(isolated, {"OPP_BOT_TOKEN": TOKEN, "ADMIN_TG_ID": "777"}, backup=False)
    assert _doctor(["--send-test"]) == 0
    assert ("sendMessage", {"chat_id": 777, "text": "SERM doctor: test alert"}) in tg.calls


# --- init -------------------------------------------------------------------

def _init(argv, tmp_path):
    return cli.cmd_init(cli.build_parser().parse_args(
        ["init", *argv, "--profiles-dir", str(tmp_path / "profiles"), "--env-file", str(tmp_path / "env")]))


def test_init_from_example_writes_valid_profile(tmp_path, isolated):
    isolated.write_text("# keep me\nUNKNOWN_KEY=1\n", encoding="utf-8")
    argv = ["--from-example", "company", "--name", "ООО Тест", "--channel", "@test_chan", "--contact", "@test_contact",
            "--slug", "acme", "--platforms", "vc"]
    assert _init(argv, tmp_path) == 0
    profile = load_profile(tmp_path / "profiles" / "acme.yaml")
    assert (profile.kind, profile.name, profile.telegram.channel) == ("company", "ООО Тест", "@test_chan")
    assert [k for k, p in profile.platforms.items() if p.enabled] == ["vc"]
    env_text = isolated.read_text(encoding="utf-8")
    assert "# keep me" in env_text and "UNKNOWN_KEY=1" in env_text
    assert envfile.read(isolated)["SERM_PROFILE"] == "acme"


def test_init_gives_own_templates_so_doctor_and_start_pass(tmp_path, isolated, monkeypatch, capsys):
    # init -> own profile; without profiles/<slug>/templates the daemon refuses to start (RT-H2)
    import asyncio
    import scheduler

    class Reached(Exception):
        pass

    argv = ["--from-example", "company", "--name", "ООО Тест", "--channel", "@test_chan", "--contact", "@test_contact",
            "--slug", "acme", "--platforms", "vc"]
    assert _init(argv, tmp_path) == 0
    templates = tmp_path / "profiles" / "acme" / "templates"
    assert sorted(p.name for p in templates.iterdir()) == sorted(cli.INIT_TEMPLATE_DIRS)
    assert (templates / "bot").is_dir() and not (templates / "admin").exists() and not (templates / "prompts").exists()
    assert "profiles/acme/templates/" in capsys.readouterr().out

    monkeypatch.setenv("SERM_PROFILE", str(tmp_path / "profiles" / "acme.yaml"))
    get_profile.cache_clear()
    _doctor(["--offline"])
    out = capsys.readouterr().out
    assert re.search(r"OK\s+Шаблоны профиля", out) and not re.search(r"FAIL\s+Шаблоны", out)

    profile = load_profile(tmp_path / "profiles" / "acme.yaml")
    monkeypatch.setattr(scheduler, "get_profile", lambda: profile)
    monkeypatch.setattr(scheduler, "BotSupervisor", lambda *a, **kw: (_ for _ in ()).throw(Reached()))
    with pytest.raises(Reached):  # got past the templates check
        asyncio.run(scheduler.main())


def test_init_keeps_existing_templates_without_confirmation(tmp_path, isolated):
    own = tmp_path / "profiles" / "petr" / "templates" / "bot" / "start.md.j2"
    own.parent.mkdir(parents=True)
    own.write_text("мой текст", encoding="utf-8")
    argv = ["--from-example", "person", "--name", "Пётр Тестов", "--channel", "@test_chan", "--contact", "@test_contact",
            "--slug", "petr"]
    assert _init(argv, tmp_path) == 0
    assert own.read_text(encoding="utf-8") == "мой текст"
    assert [p.name for p in (tmp_path / "profiles" / "petr" / "templates").iterdir()] == ["bot"]


def test_init_does_not_overwrite_without_confirmation(tmp_path, isolated):
    argv = ["--from-example", "person", "--name", "Пётр Тестов", "--channel", "@test_chan", "--contact", "@test_contact",
            "--slug", "petr"]
    assert _init(argv, tmp_path) == 0
    path = tmp_path / "profiles" / "petr.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")
    envfile.write(isolated, {"SERM_PROFILE": "other"}, backup=False)
    assert _init(argv, tmp_path) == 1
    assert path.read_text(encoding="utf-8").endswith("# edited by hand\n")
    assert envfile.read(isolated)["SERM_PROFILE"] == "other"


def test_init_interactive_stores_secrets_without_printing(tmp_path, isolated, tg, monkeypatch, capsys):
    envfile.write(isolated, {"POLZA_API_KEY": "old-key-value"}, backup=False)
    answers = iter(["company", "ООО Ромашка-Тест", "", "@test_chan", "@test_contact", "vc,spark", "romashka",
                    "n"])  # last: keep the existing LLM key
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    secrets = iter([TOKEN, "new-llm-key-0000"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(secrets))
    assert _init([], tmp_path) == 0
    env = envfile.read(isolated)
    assert env["OPP_BOT_TOKEN"] == TOKEN and env["POLZA_API_KEY"] == "old-key-value"
    assert load_profile(tmp_path / "profiles" / "romashka.yaml").telegram.bot_username == "test_brand_bot"
    out = capsys.readouterr().out
    assert TOKEN not in out and "new-llm-key-0000" not in out and "old-key-value" not in out


def test_init_rejects_example_slug(tmp_path):
    argv = ["--from-example", "person", "--name", "X Y", "--channel", "@test_chan", "--contact", "@test_contact",
            "--slug", "example-me"]
    assert _init(argv, tmp_path) == 1
    assert not (tmp_path / "profiles").exists()


# --- small commands ---------------------------------------------------------

def test_env_example_matches_schema(capsys):
    assert cli.cmd_env_example(None) == 0
    assert capsys.readouterr().out == settings.render_env_example()


def test_llm_usage_summary(tmp_path, capsys):
    rows = [
        {"campaign_id": "c1", "model": "m", "tokens": {"prompt": 100, "completion": 50}, "cost": 0.5, "cached": False},
        {"campaign_id": "c1", "model": "m", "tokens": 150, "cost": 0, "cached": True},
        {"campaign_id": "c2", "model": "m", "prompt_tokens": 10, "completion_tokens": 5, "cost": None, "cached": False},
    ]
    (tmp_path / "llm_usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nbroken\n", encoding="utf-8")
    assert cli.cmd_llm_usage(SimpleNamespace(campaign=None)) == 0
    out = capsys.readouterr().out
    assert re.search(r"c1\s+\|\s+m\s+\|\s+2 \|\s+1 \|\s+300 \|\s+0\.5000$", out, re.M)
    assert re.search(r"c2\s+\|\s+m\s+\|\s+1 \|\s+0 \|\s+15 \|\s+0\.0000\*", out)
    assert "Итого: 0.5000" in out and "битых строк: 1" in out


@pytest.mark.parametrize("argv", [["login", "spark"], ["login", "--platform", "spark"]])
def test_login_via_registry_exports_cookies(argv, monkeypatch):
    seen = []
    monkeypatch.setattr(cli.registry, "get_publisher",
                        lambda key: SimpleNamespace(interactive_login=lambda: seen.append(key)))
    from core.session_manager import SessionManager

    async def export(platform):
        seen.append(f"export:{platform}")
        return None

    monkeypatch.setattr(SessionManager, "export_storage_state", staticmethod(export))
    assert cli.cmd_login(cli.build_parser().parse_args(argv)) == 0
    assert seen == ["spark", "export:spark"]


def test_run_opp_bot_defaults_from_profile(monkeypatch):
    started = []

    async def fake_run(token, channel, pdf, contact):
        started.append((channel, contact))

    # A stand-in module: the real core.opp_bot resolves a profile at import time, so importing it here
    # would make the result depend on which test imported it first and with which SERM_PROFILE
    monkeypatch.setitem(sys.modules, "core.opp_bot", SimpleNamespace(run_opp_bot=fake_run))
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    cli.cmd_run_opp_bot(cli.build_parser().parse_args(["run-opp-bot", "--token", TOKEN]))
    monkeypatch.setenv("OPP_CHANNEL", "@env_channel")
    cli.cmd_run_opp_bot(cli.build_parser().parse_args(["run-opp-bot", "--token", TOKEN]))
    assert started == [("@example_channel", "@example_contact"), ("@env_channel", "@example_contact")]


# --- browser extension ------------------------------------------------------

def test_extension_has_no_built_in_server_or_token():
    ext = PROJECT_ROOT / "extension"
    for name in ("background.js", "popup.js"):
        text = (ext / name).read_text(encoding="utf-8")
        assert re.search(r'serverUrl:\s*""', text) and re.search(r'adminToken:\s*""', text), name
    for path in ext.glob("*.*"):
        if path.suffix in (".js", ".html", ".json"):
            text = path.read_text(encoding="utf-8")
            assert not IPV4_RE.search(text), path.name
            assert not re.search(r"\b[0-9a-f]{32}\b", text), path.name  # hex admin token
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    # required hosts: only platform cookie domains and local servers; the user's server is optional
    local = {"http://localhost:*/*", "http://127.0.0.1:*/*"}
    platform_domains = ("vc.ru", "spark.ru", "dzen.ru", "e-xecutive.ru", "habr.com", "rb.ru")
    assert all(h in local or h in {f"*://*.{d}/*" for d in platform_domains} for h in manifest["host_permissions"])
    assert manifest["optional_host_permissions"] == ["https://*/*", "http://*/*"]
