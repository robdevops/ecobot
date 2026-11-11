"""The two programs as they are installed: two Docker images from one Dockerfile (ecobot-mcp with only its own packages, ecobot with
everything), compose running each from its own, and the systemd unit of ecobot-mcp without the bot's secrets, by their new and old names.
(Read as text: the CI workflow docker.yml builds and runs the images themselves.)"""

import re
from dataclasses import fields
from pathlib import Path

from lib.config import Config

ROOT = Path(__file__).resolve().parent.parent


def requirements(name: str) -> set[str]:
    found = set()
    for line in (ROOT / name).read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("-r "):
            found |= requirements(line[3:].strip())
        elif line:
            found.add(re.split(r"[=<>!\[ ]", line)[0].lower())
    return found


def test_ecobot_mcp_needs_no_telegram_and_no_model_client_and_the_bot_has_everything():
    mcp, bot = requirements("requirements-mcp.txt"), requirements("requirements.txt")
    assert {"mcp", "uvicorn", "httpx"} <= mcp and not {"python-telegram-bot", "openai"} & mcp
    assert mcp < bot and {"python-telegram-bot", "openai"} <= bot


def test_one_dockerfile_builds_both_images_each_checked_at_build():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM app AS ecobot-mcp$", text, re.M) and re.search(r"^FROM app AS ecobot$", text, re.M)
    mcp, bot = text.split("FROM app AS ecobot-mcp")[1].split("FROM app AS ecobot\n")
    assert "requirements-mcp.txt" in text.split("FROM base AS venv-mcp")[1].split("FROM base AS venv-bot")[0]
    assert 'CMD ["python", "-m", "ecobot_mcp"]' in mcp and "import ecobot_mcp.__main__" in mcp and "find_spec('telegram') is None" in mcp
    assert 'CMD ["python", "ecobot.py"]' in bot and "import lib.bot, ecobot" in bot
    assert "requirements-mcp.txt" in (ROOT / ".dockerignore").read_text()


def test_compose_runs_each_program_from_its_own_image():
    text = (ROOT / "compose.yaml").read_text()
    services = dict(re.findall(r"^  ([\w-]+):\n((?:    .*\n|\s*\n)+)", text, re.M))
    mcp, bot = services["ecobot-mcp"], services["ecobot"]
    assert "image: ecobot-mcp" in mcp and "target: ecobot-mcp" in mcp and "hostname: ecobot-mcp" in mcp and "- 8765:8765" in mcp
    assert "image: ecobot\n" in bot and "target: ecobot\n" in bot and "profiles: [bot]" in bot and "depends_on: [ecobot-mcp]" in bot
    assert "BOT_MCP_URL=http://ecobot-mcp:8765/mcp" in (ROOT / "docker" / "bot.env").read_text()


def test_the_ecobot_mcp_unit_drops_the_bots_secrets_by_their_new_and_old_names():
    text = (ROOT / "ecobot-mcp.service").read_text()
    unset = set(re.search(r"^UnsetEnvironment=(.*)$", text, re.M)[1].split())
    by_env = {f.metadata["env"]: f.metadata.get("old") for f in fields(Config) if f.metadata.get("env")}
    for name in ("BOT_TELEGRAM_TOKEN", "BOT_OPENROUTER_API_KEY", "BOT_TUYA_CLIENT_ID", "BOT_TUYA_CLIENT_SECRET"):
        assert {name, by_env[name]} <= unset, name
    assert "AUTH_TOKENS" not in unset and "ExecStart=/opt/ecobot/.venv/bin/python -m ecobot_mcp" in text


def test_an_old_ecobot_service_unit_still_starts_ecobot_mcp_without_the_bots_secrets_by_their_new_names(monkeypatch):
    # an ecobot-mcp.service copied in before the rename runs python -m ecobot_service and unsets the bot's secrets by their old names
    import ecobot_mcp.__main__
    import ecobot_service.__main__ as old
    unit = set(re.search(r"^UnsetEnvironment=(.*)$", (ROOT / "ecobot-mcp.service").read_text(), re.M)[1].split())
    assert set(old.BOT_SECRETS) <= unit
    environ = {name: "secret" for name in old.BOT_SECRETS} | {"MCP_ECOWITT_API_KEY": "k", "AUTH_TOKENS": "t"}
    old.drop_bot_secrets(environ)
    assert environ == {"MCP_ECOWITT_API_KEY": "k", "AUTH_TOKENS": "t"}
    ran = []
    monkeypatch.setattr(ecobot_mcp.__main__, "run", lambda: ran.append(True))
    monkeypatch.setenv("BOT_TELEGRAM_TOKEN", "secret")
    old.run()
    assert ran == [True] and "BOT_TELEGRAM_TOKEN" not in __import__("os").environ


def test_the_systemd_upgrade_copies_the_ecobot_mcp_unit_in_again():
    text = (ROOT / "README.md").read_text()
    upgrade = text.split("### systemd", 2)[2].split("### Docker")[0]
    assert "sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && " \
           "sudo systemctl restart ecobot-mcp" in upgrade
