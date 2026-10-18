"""
examples/deploy/rsync-deploy.sh end to end on a local "server": fake `ssh` runs the remote command
with bash on this machine, fake `docker` records its calls (doctor result is set per test), rsync is real.
The repo is a throwaway git copy holding the script and a few files; nothing leaves tmp_path.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "examples" / "deploy" / "rsync-deploy.sh"

FAKE_SSH = r"""#!/usr/bin/env bash
# ssh [options] [user@]host command...: run the command locally, like a remote login shell.
# The destination arrives per rsync build: openrsync (macOS) passes "user@host", rsync 3.2.x (Linux)
# splits it into "-l user host" - so options are skipped the way ssh parses them, not up to the first word.
while [ $# -gt 0 ]; do
  case "$1" in
    -i) printf '%s\n' "$2" >> "$FAKE_LOG_DIR/ssh-keys"; shift 2 ;;
    -i?*) printf '%s\n' "${1#-i}" >> "$FAKE_LOG_DIR/ssh-keys"; shift ;;
    -[BbcDEeFIJLlmOoPpQRSWw]) shift 2 ;;  # options with a value: -l user, -p 22, -o opt, -e char
    --) shift; break ;;
    -?*) shift ;;  # flags (-4 -A -C -q -T -x ...) and glued values (-p22, -oBatchMode=yes)
    *) break ;;
  esac
done
shift  # host
exec bash -c "$*"
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG_DIR/docker"
case "$*" in
  *"cli.py doctor"*)
    [ -e "$FAKE_LOG_DIR/doctor-fails" ] && { echo "FAIL  Профиль" ; exit 1; }
    ;;
esac
exit 0
"""


def _git(repo, *args):
    subprocess.check_call(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=repo,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def env(tmp_path):
    if not shutil.which("rsync") or not shutil.which("git"):
        pytest.skip("rsync and git are needed")
    bin_dir, logs = tmp_path / "bin", tmp_path / "logs"
    bin_dir.mkdir()
    logs.mkdir()
    for name, text in (("ssh", FAKE_SSH), ("docker", FAKE_DOCKER)):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)

    repo = tmp_path / "repo"
    (repo / "examples" / "deploy").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "examples" / "deploy" / "rsync-deploy.sh")
    (repo / "app.py").write_text("NEW = True\n", encoding="utf-8")
    (repo / "profiles").mkdir()
    (repo / "profiles" / "local-only.yaml").write_text("slug: local-only\n", encoding="utf-8")
    (repo / ".gitignore").write_text("profiles/local-only.yaml\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    server = tmp_path / "srv" / "serm"
    (server / "data").mkdir(parents=True)
    (server / "data" / "serm.db").write_text("db", encoding="utf-8")
    (server / ".env").write_text("OPP_CHANNEL=@server\n", encoding="utf-8")
    (server / "profiles" / "brand" / "templates" / "bot").mkdir(parents=True)
    (server / "profiles" / "brand" / "templates" / "bot" / "start.j2").write_text("hi\n", encoding="utf-8")
    (server / "profiles" / "brand.yaml").write_text("slug: brand\ntemplates_dir: null\n", encoding="utf-8")
    (server / "profiles" / "example-person.yaml").write_text("slug: example-person\n", encoding="utf-8")
    (server / "app.py").write_text("NEW = False\n", encoding="utf-8")
    (server / "stale.py").write_text("old\n", encoding="utf-8")

    key = tmp_path / "keys dir" / "id test"  # CFG-L4: spaces in the key path
    key.parent.mkdir()
    key.write_text("not a key", encoding="utf-8")

    run_env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_LOG_DIR=str(logs))

    def deploy(*extra):
        return subprocess.run(
            ["bash", "examples/deploy/rsync-deploy.sh", "--host", "deploy@server", "--key", str(key),
             "--dir", str(server), "--yes", *extra],
            cwd=repo, env=run_env, capture_output=True, text=True, timeout=120)

    def docker_calls():
        path = logs / "docker"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    return type("Env", (), dict(deploy=staticmethod(deploy), server=server, logs=logs, key=key,
                                bin_dir=bin_dir, run_env=run_env, docker_calls=staticmethod(docker_calls)))


def _state_kept(server):
    assert (server / "data" / "serm.db").read_text(encoding="utf-8") == "db"
    assert (server / ".env").read_text(encoding="utf-8") == "OPP_CHANNEL=@server\n"
    assert (server / "profiles" / "brand.yaml").is_file() and (server / "profiles" / "brand" / "templates").is_dir()
    assert not (server / "profiles" / "local-only.yaml").exists()


@pytest.mark.parametrize("argv", [
    ["-o", "BatchMode=yes", "-i", "/k ey", "deploy@server", "echo", "ran"],  # openrsync, the script's own ssh
    ["-o", "BatchMode=yes", "-i", "/k ey", "-l", "deploy", "server", "echo", "ran"],  # rsync 3.2.x
    ["-i/k ey", "-p", "22", "-oConnectTimeout=10", "-x", "-T", "-e", "none", "-l", "deploy", "--", "server",
     "echo", "ran"],
])
def test_fake_ssh_finds_host_in_both_rsync_forms(env, argv):
    # CI on ubuntu: rsync 3.2.7 passes "-l deploy server", the old fake took "-l" for the host and ran "deploy"
    ssh = env.bin_dir / "ssh"
    res = subprocess.run([str(ssh), *argv], env=env.run_env, capture_output=True, text=True, timeout=10)
    assert (res.returncode, res.stdout, res.stderr) == (0, "ran\n", "")
    assert (env.logs / "ssh-keys").read_text(encoding="utf-8") == "/k ey\n"


def test_deploy_checks_new_code_before_replacing(env):
    res = env.deploy()
    assert res.returncode == 0, res.stderr
    assert (env.server / "app.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert not (env.server / "stale.py").exists()
    _state_kept(env.server)
    calls = env.docker_calls()
    staging = f"{env.server}.staging"
    assert calls[0] == f"build -t serm-engine:staging {staging}"
    assert "cli.py doctor --offline" in calls[1] and f"{env.server}/.env:/app/.env" in calls[1]
    assert calls[2:4] == ["compose build", "compose up -d --remove-orphans"]


def test_failed_doctor_leaves_server_untouched(env):
    # CFG-M5: before, rsync replaced the code first and a failed doctor left new code + old container
    (env.logs / "doctor-fails").write_text("", encoding="utf-8")
    res = env.deploy()
    assert res.returncode != 0
    assert (env.server / "app.py").read_text(encoding="utf-8") == "NEW = False\n"
    assert (env.server / "stale.py").exists()
    _state_kept(env.server)
    assert not any(c.startswith("compose up") for c in env.docker_calls())
    assert "unchanged" in res.stderr


def test_own_profile_without_templates_is_refused(env):
    # CFG-H2: an own profile without its templates would make the daemon speak with the neutral scaffold
    shutil.rmtree(env.server / "profiles" / "brand")
    res = env.deploy()
    assert res.returncode != 0
    assert "profiles/brand/templates" in res.stderr
    assert (env.server / "stale.py").exists() and env.docker_calls() == []
    assert not Path(f"{env.server}.staging").exists()


def test_own_profile_with_empty_templates_dir_is_refused(env):
    # N5: an empty templates dir passes [ -d ] but leaves the daemon without templates
    shutil.rmtree(env.server / "profiles" / "brand" / "templates")
    (env.server / "profiles" / "brand" / "templates" / "bot").mkdir(parents=True)
    res = env.deploy()
    assert res.returncode != 0
    assert "profiles/brand/templates" in res.stderr
    assert (env.server / "stale.py").exists() and env.docker_calls() == []


def test_explicit_templates_dir_is_left_to_doctor(env):
    shutil.rmtree(env.server / "profiles" / "brand")
    (env.server / "profiles" / "brand.yaml").write_text("slug: brand\ntemplates_dir: /elsewhere  # x\n",
                                                        encoding="utf-8")
    res = env.deploy()
    assert res.returncode == 0, res.stderr


def test_key_path_with_spaces_reaches_ssh(env):
    res = env.deploy()
    assert res.returncode == 0, res.stderr
    keys = set((env.logs / "ssh-keys").read_text(encoding="utf-8").splitlines())
    assert keys == {str(env.key)}  # every ssh call, rsync's included, got the whole path


def test_skip_doctor(env):
    res = env.deploy("--skip-doctor")
    assert res.returncode == 0, res.stderr
    assert not any("doctor" in c for c in env.docker_calls())
    assert (env.server / "app.py").read_text(encoding="utf-8") == "NEW = True\n"
