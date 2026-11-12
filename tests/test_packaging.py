"""The programs as they are installed: three Docker images from one Dockerfile (each with only its own packages: the bot's no Pillow
and no source's, docs/ecobot-mcp.md step 6b; irrigation-mcp's no Telegram, no model client, no Pillow, step 8), compose running each from
its own, and the systemd units: ecobot-mcp's without the bot's
secrets, by their new and old names, and the bot's wanting and started after ecobot-mcp's. (Read as text: the CI workflow docker.yml
builds and runs the images themselves.)"""

import re
from dataclasses import fields
from pathlib import Path

from lib.config import Config, old_names

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


def test_ecobot_mcp_needs_no_telegram_and_no_model_client_and_the_bot_only_its_own_packages():
    mcp, bot, both = requirements("requirements-mcp.txt"), requirements("requirements-bot.txt"), requirements("requirements.txt")
    irrigation = requirements("requirements-irrigation.txt")
    assert {"mcp", "uvicorn", "httpx"} <= mcp and not {"python-telegram-bot", "openai"} & mcp
    assert bot == {"python-telegram-bot", "openai", "mcp", "httpx"}            # no Pillow, no source's packages (holidays, zeroconf)
    assert irrigation == {"mcp", "uvicorn", "httpx"}                           # irrigation-mcp: the server, its client, Tuya's client
    assert both == mcp | bot | irrigation                                      # (a systemd install's one venv: every program)
    pins = lambda name: {line.split("#")[0].strip() for line in (ROOT / name).read_text().splitlines() if line.startswith(("mcp", "uvicorn"))}
    assert {p for p in pins("requirements-mcp.txt") if p.startswith("mcp")} == pins("requirements-bot.txt")   # the sides of MCP upgraded together
    assert pins("requirements-irrigation.txt") == pins("requirements-mcp.txt")


def test_one_dockerfile_builds_both_images_each_checked_at_build():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM app AS ecobot-mcp$", text, re.M) and re.search(r"^FROM app AS ecobot$", text, re.M)
    mcp, bot = text.split("FROM app AS ecobot-mcp")[1].split("FROM app AS ecobot\n")
    mcp, irrigation = mcp.split("FROM app AS irrigation-mcp")
    assert "requirements-mcp.txt" in text.split("FROM base AS venv-mcp")[1].split("FROM base AS venv-bot")[0]
    venv_bot = text.split("FROM base AS venv-bot")[1].split("FROM base AS venv-irrigation")[0]
    venv_irrigation = text.split("FROM base AS venv-irrigation")[1].split("FROM base AS app")[0]
    assert "-r /tmp/requirements-irrigation.txt" in venv_irrigation and "requirements-charts" not in venv_irrigation
    assert "-r /tmp/requirements-bot.txt" in venv_bot and not re.search(r"requirements(-mcp|-charts)?\.txt", venv_bot.replace("requirements-bot.txt", ""))
    assert 'CMD ["python", "-m", "ecobot_mcp"]' in mcp and "import ecobot_mcp.__main__" in mcp and "find_spec('telegram') is None" in mcp
    assert 'CMD ["python", "ecobot.py"]' in bot and "import lib.bot, ecobot" in bot and "find_spec('PIL') is None" in bot
    assert all(f"'{m}'" in bot for m in ("lib.ecowitt", "lib.airgradient", "lib.forecast", "lib.pollen", "lib.storage"))
    assert 'CMD ["python", "-m", "irrigation_mcp"]' in irrigation and "import irrigation_mcp.__main__" in irrigation
    assert "find_spec('telegram') is None" in irrigation and "find_spec('PIL') is None" in irrigation and "'lib.storage'" in irrigation
    ignored = (ROOT / ".dockerignore").read_text()
    assert all(f"!{name}" in ignored for name in ("requirements-mcp.txt", "requirements-bot.txt", "requirements-irrigation.txt", "irrigation_mcp/"))


def test_compose_runs_each_program_from_its_own_image():
    text = (ROOT / "compose.yaml").read_text()
    services = dict(re.findall(r"^  ([\w-]+):\n((?:    .*\n|\s*\n)+)", text, re.M))
    mcp, bot, irrigation = services["ecobot-mcp"], services["ecobot"], services["irrigation-mcp"]
    assert "image: irrigation-mcp" in irrigation and "target: irrigation-mcp" in irrigation and "profiles: [irrigation]" in irrigation
    assert "IRRIGATION_BIND: 0.0.0.0:8766" in irrigation and "- 8766:8766" in irrigation and "docker/irrigation.env" in irrigation
    assert "IRRIGATION_ECOBOT_MCP_URL=http://ecobot-mcp:8765/mcp" in (ROOT / "docker" / "irrigation.env").read_text()
    assert "image: ecobot-mcp" in mcp and "target: ecobot-mcp" in mcp and "hostname: ecobot-mcp" in mcp and "- 8765:8765" in mcp
    assert "image: ecobot\n" in bot and "target: ecobot\n" in bot and "profiles: [bot]" in bot and "depends_on: [ecobot-mcp]" in bot
    assert "BOT_MCP_URL=http://ecobot-mcp:8765/mcp" in (ROOT / "docker" / "bot.env").read_text()


def test_the_ecobot_mcp_unit_drops_the_bots_secrets_by_their_new_and_old_names():
    text = (ROOT / "ecobot-mcp.service").read_text()
    unset = set(re.search(r"^UnsetEnvironment=(.*)$", text, re.M)[1].split())
    by_env = {f.metadata["env"]: old_names(f) for f in fields(Config) if f.metadata.get("env")}
    for name in ("BOT_TELEGRAM_TOKEN", "BOT_OPENROUTER_API_KEY", "IRRIGATION_TUYA_CLIENT_ID", "IRRIGATION_TUYA_CLIENT_SECRET"):
        assert {name, *by_env[name]} <= unset, name
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
    assert "sudo cp /opt/ecobot/ecobot-mcp.service /opt/ecobot/ecobot.service /etc/systemd/system/ && sudo systemctl daemon-reload && " \
           "sudo systemctl restart ecobot-mcp ecobot" in upgrade
    assert "BOT_MCP_URL=http://127.0.0.1:8765/mcp" in upgrade and "sudo systemctl enable --now ecobot-mcp.service" in upgrade   # (a bot
                                                                                                                            # that ran alone)


def test_the_bots_unit_wants_ecobot_mcps_and_starts_after_it_and_the_readme_installs_both():
    """Under systemd the bot is ecobot-mcp's client (step 6b): its unit pulls ecobot-mcp's in and starts after it, and the install
    enables both, ecobot-mcp first."""
    unit = (ROOT / "ecobot.service").read_text()
    wants = re.search(r"^Wants=(.*)$", unit, re.M)[1].split()
    after = re.search(r"^After=(.*)$", unit, re.M)[1].split()
    assert "ecobot-mcp.service" in wants and "ecobot-mcp.service" in after
    readme = (ROOT / "README.md").read_text()
    install = readme.split("### systemd", 1)[1].split("### ", 1)[0]
    assert "sudo systemctl enable --now ecobot-mcp.service ecobot.service" in install                     # ecobot-mcp first
    assert "/opt/ecobot/ecobot-mcp.service /opt/ecobot/ecobot.service /etc/systemd/system/" in install
    assert "BOT_MCP_URL=http://127.0.0.1:8765/mcp" in install and "on its own (optional)" not in readme
