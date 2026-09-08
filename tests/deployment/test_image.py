"""从 Docker 构建白名单组成隔离目录，验证完整应用导入与命令入口。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def image_root(tmp_path):
    context = tmp_path / "image"
    context.mkdir()
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    assert rules[0] == "*"
    for rule in rules:
        if rule.startswith("!"):
            source = ROOT / rule[1:]
            shutil.copytree(
                source,
                context / source.name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
    assert not (context / ".env.example").exists()
    assert not (context / "tests").exists()
    return context


def probe(context, code):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);\n" + code,
            str(context),
        ],
        cwd=context,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_image_imports_and_registers_handlers(image_root):
    result = probe(
        image_root,
        """
import asyncio
from tgforward import app, __version__
from pathlib import Path
import tgforward
assert Path(tgforward.__file__).parent.parent == Path(sys.argv[1])
assert app._check_config()
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
app.load_handlers()
from tgforward.telegram.clients import bot
loop.run_until_complete(asyncio.sleep(0))
callbacks = [getattr(handler, 'original_callback', None)
             for group in bot.dispatcher.groups.values() for handler in group]
assert any(getattr(callback, '__name__', '') == 'start_handler' for callback in callbacks)
pending = asyncio.all_tasks(loop)
for task in pending:
    task.cancel()
loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
loop.close()
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "module,args",
    [
        ("tgforward", ["--version"]),
        ("tgforward.tools.import_mongo", ["--help"]),
    ],
)
def test_image_command_entrypoints(image_root, module, args):
    result = probe(
        image_root,
        f"""
import runpy
sys.argv = [{module!r}, *{args!r}]
runpy.run_module({module!r}, run_name='__main__')
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip()


def test_dockerfile_runs_the_package_and_healthcheck():
    text = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "tgforward"]' in text
    assert "python -m tgforward.runtime.healthcheck" in text
    assert "COPY tgforward ./tgforward" in text
    assert "requirements/runtime.lock" in text
