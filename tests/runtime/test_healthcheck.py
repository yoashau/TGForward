"""就绪状态与心跳检测。"""

import os

import pytest

from tgforward.runtime.healthcheck import healthy


@pytest.mark.parametrize(
    "ready,age,expected",
    [
        (False, 0, False),
        (True, 0, True),
        (True, 89, True),
        (True, 90, False),
        (True, 300, False),
        (True, -10, False),
    ],
)
def test_health_requires_readiness_and_fresh_heartbeat(tmp_path, ready, age, expected):
    if ready:
        (tmp_path / ".ready").write_text(str(os.getpid()))
    beat = tmp_path / ".heartbeat"
    beat.write_text("fixture")
    os.utime(beat, (1000 - age, 1000 - age))
    assert healthy(tmp_path, now=1000) is expected


def test_health_rejects_invalid_ready_pid(tmp_path):
    (tmp_path / ".ready").write_text("not ready")
    assert not healthy(tmp_path)
