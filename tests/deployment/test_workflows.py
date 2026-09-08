"""同一提交通过测试后发布镜像，部署数据不受构建影响。"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_publish_requires_checks_for_same_commit():
    ci = workflow("ci.yml")
    assert ci["jobs"]["publish"]["needs"] == "checks"
    assert ci["jobs"]["publish"]["if"] == "github.event_name == 'push'"
    assert ci["jobs"]["publish"]["uses"] == "./.github/workflows/image.yml"
    image = workflow("image.yml")
    assert set(image["on"]) == {"workflow_call"}
    steps = image["jobs"]["image"]["steps"]
    build = next(
        step for step in steps if step.get("uses", "").startswith("docker/build-push-action")
    )
    assert build["with"]["push"] == "true"
    assert "github.sha" in build["with"]["build-args"]


def test_ci_checks_code_version_and_production_entrypoint():
    commands = "\n".join(s.get("run", "") for s in workflow("ci.yml")["jobs"]["checks"]["steps"])
    for expected in (
        "ruff check .",
        "python -m pytest",
        "bash -n scripts/deploy.sh",
        "docker build",
        "tgforward --version",
        "GITHUB_REF_NAME#v",
    ):
        assert expected in commands


def test_compose_keeps_config_and_data_outside_checkout():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["name"] == "tgforward"
    assert set(compose["services"]) == {"bot"}
    assert "volumes" not in compose
    bot = compose["services"]["bot"]
    assert "depends_on" not in bot
    volume = bot["volumes"][0]
    assert volume["type"] == "bind" and volume["target"] == "/app/data"
    assert "/var/lib/tgforward" in volume["source"]
    assert volume["bind"]["create_host_path"] is False
    assert "/etc/tgforward/tgforward.env" in bot["env_file"][0]
    assert "10001" in bot["build"]["args"]["APP_UID"]
    assert "10001" in bot["build"]["args"]["APP_GID"]
