"""预检应在创建外部资源之前完成，错误值不在 import 阶段崩溃。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALID = dict(
    API_ID="12345",
    API_HASH="test",
    BOT_TOKEN="123456:test",
    OWNER_ID="1",
    STRING="",
    MASTER_KEY="explicit-test-only-master-key-0123456789abcdef",
    SALT_KEY="explicit-test-only-salt",
)


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"API_ID": "not-a-number"}, "API_ID"),
        ({"OWNER_ID": "0 -1"}, "OWNER_ID"),
        ({"MASTER_KEY": ""}, "MASTER_KEY"),
        ({"SALT_KEY": "", "IV_KEY": ""}, "SALT_KEY"),
        ({"MASTER_KEY": "gK8HzLfT9QpViJcYeB5wRa3DmN7P2xUq"}, "MASTER_KEY"),
        ({"SALT_KEY": "s7Yx5CpVmE3F"}, "SALT_KEY"),
        ({"MASTER_KEY": "short-key"}, "MASTER_KEY"),
    ],
)
def test_invalid_config_exits_without_resource_imports(overrides, field):
    code = """
import sys
from tgforward import app as main
assert main.run() == 1
assert 'tgforward.telegram.clients' not in sys.modules
assert 'tgforward.handlers' not in sys.modules
assert 'tgforward.storage.sqlite' not in sys.modules
assert 'motor.motor_asyncio' not in sys.modules
print('preflight=PASS')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, **VALID, **overrides},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert field in result.stderr and "preflight=PASS" in result.stdout
    assert "Traceback" not in result.stderr


def test_sqlite_config_needs_no_mongo_or_network():
    code = """
import socket
socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(AssertionError('DNS during preflight'))
from tgforward import config
assert not config.validate_config(), config.validate_config()
from tgforward.storage import sqlite
assert sqlite._store is None
print('lazy=PASS')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, **VALID, "MONGO_DB": "mongodb+srv://cluster.example.invalid"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
