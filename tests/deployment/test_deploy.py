"""部署入口的配置保护、状态初始化和失败传播。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
case " $* " in
  *" tgforward.tools.init_state "*)
    DATA_DIR="$TGFORWARD_DATA_DIR" "$PYTHON" -m tgforward.tools.init_state ;;
  *" up "*) test "${FAIL_HEALTH:-0}" = 0 ;;
esac
"""


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/deploy.sh", root / "scripts/deploy.sh")
    shutil.copyfile(ROOT / ".env.example", root / ".env.example")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ)
    for name in (
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "OWNER_ID",
        "MASTER_KEY",
        "SALT_KEY",
        "IV_KEY",
        "DATA_DIR",
        "STRING",
        "LOG_GROUP",
    ):
        env.pop(name, None)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        PYTHON=sys.executable,
        PYTHONPATH=str(ROOT),
        APP_UID=str(os.getuid()),
        APP_GID=str(os.getgid()),
        TGFORWARD_ENV_FILE=str(tmp_path / "config" / "bot.env"),
        TGFORWARD_DATA_DIR=str(tmp_path / "data"),
        DOCKER_CALLS=str(tmp_path / "calls"),
    )
    return root, env


def deploy(deployment, mode="update", **changes):
    root, env = deployment
    return subprocess.run(
        ["bash", str(root / "scripts/deploy.sh"), mode],
        env={**env, **changes},
        cwd=root.parent,
        capture_output=True,
        text=True,
        timeout=15,
    )


def configure(deployment):
    assert deploy(deployment, "init").returncode == 2
    _, env = deployment
    config = Path(env["TGFORWARD_ENV_FILE"])
    text = config.read_text()
    for name, value in {
        "API_ID": "12345",
        "API_HASH": "test",
        "BOT_TOKEN": "123456:test",
        "OWNER_ID": "1",
    }.items():
        text = text.replace(f"\n{name}=\n", f"\n{name}={value}\n")
    config.write_text(text)
    return config


def test_initial_setup_creates_private_config_without_starting_bot(deployment):
    result = deploy(deployment, "init")
    assert result.returncode == 2
    _, env = deployment
    config = Path(env["TGFORWARD_ENV_FILE"])
    assert config.stat().st_mode & 0o777 == 0o600
    values = dict(
        line.split("=", 1)
        for line in config.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert len(values["MASTER_KEY"]) >= 32 and values["SALT_KEY"]
    assert values["MASTER_KEY"] not in result.stdout and values["SALT_KEY"] not in result.stdout
    assert not Path(env["DOCKER_CALLS"]).exists()


def test_init_validates_config_and_initializes_real_database(deployment):
    config = configure(deployment)
    original = config.read_bytes()
    result = deploy(deployment, "init")
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == original
    _, env = deployment
    assert (Path(env["TGFORWARD_DATA_DIR"]) / "state/tgforward.sqlite3").is_file()
    calls = Path(env["DOCKER_CALLS"]).read_text()
    assert "build bot" in calls and "tgforward.tools.init_state" in calls
    assert "up -d --wait --wait-timeout 180 bot" in calls
    assert "--force-recreate" not in calls


def test_repeated_init_preserves_keys_and_existing_users(deployment):
    import sqlite3

    config = configure(deployment)
    assert deploy(deployment, "init").returncode == 0
    _, env = deployment
    database = Path(env["TGFORWARD_DATA_DIR"]) / "state/tgforward.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO users VALUES (42, 1, ?, 1)", ('{"user_id":42}',))
    before = config.read_bytes()
    assert deploy(deployment, "init").returncode == 0
    assert config.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT user_id FROM users").fetchone()[0] == 42


def test_update_only_rebuilds_and_waits_without_touching_state(deployment):
    config = configure(deployment)
    assert deploy(deployment, "init").returncode == 0
    root, env = deployment
    before = config.read_bytes()
    calls = Path(env["DOCKER_CALLS"])
    calls.unlink()
    (root / "local-work.txt").write_text("keep local edits")
    result = deploy(deployment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == before
    assert (root / "local-work.txt").read_text() == "keep local edits"
    assert len(calls.read_text().splitlines()) == 1
    assert "up -d --build --wait --wait-timeout 180 bot" in calls.read_text()


def test_missing_state_does_not_silently_initialize_an_empty_database(deployment):
    configure(deployment)
    result = deploy(deployment)
    assert result.returncode != 0
    assert not Path(deployment[1]["DOCKER_CALLS"]).exists()


def test_invalid_credentials_stop_before_bot_start(deployment):
    assert deploy(deployment, "init").returncode == 2
    result = deploy(deployment, "init")
    assert result.returncode != 0
    assert " up " not in Path(deployment[1]["DOCKER_CALLS"]).read_text()


def test_failed_healthcheck_does_not_announce_success(deployment):
    configure(deployment)
    result = deploy(deployment, "init", FAIL_HEALTH="1")
    assert result.returncode != 0
    assert "TGForward 已启动。" not in result.stdout


def test_missing_config_and_unknown_modes_stop_before_docker(deployment):
    assert deploy(deployment).returncode != 0
    assert deploy(deployment, "unknown").returncode != 0
    assert not Path(deployment[1]["DOCKER_CALLS"]).exists()
