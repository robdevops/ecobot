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
    assert all(f"'{m}'" in bot for m in ("lib.ecowitt", "lib.airgradient", "lib.forecast", "lib.pollen", "lib.storage",
                                         "lib.irrigation.tuya", "lib.irrigation.watchdog", "lib.irrigation.check"))   # (irrigation-mcp's, 8b)
    assert "lib.irrigation_remote" in bot
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


def unit(name: str) -> tuple[str, dict[str, list[str]]]:
    text = (ROOT / name).read_text()
    keys = ("Wants", "After", "Before", "UnsetEnvironment", "ExecStart", "User", "StateDirectory", "Restart", "RestartPreventExitStatus")
    return text, {k: (m[1].split() if (m := re.search(rf"^{k}=(.*)$", text, re.M)) else []) for k in keys}


def test_irrigation_mcps_unit_runs_it_after_ecobot_mcp_and_before_the_bot_without_the_others_secrets():
    """Step 8b: irrigation-mcp's own unit, from the same checkout and venv, the same dynamic user and data folder; it wants ecobot-mcp's
    (its daily check asks it) and starts before the bot's; it drops the bot's and ecobot-mcp's secrets, by every name they have had."""
    text, irrigation = unit("irrigation-mcp.service")
    assert irrigation["ExecStart"] == ["/opt/ecobot/.venv/bin/python", "-m", "irrigation_mcp"]
    assert "ecobot-mcp.service" in irrigation["Wants"] and "ecobot-mcp.service" in irrigation["After"] and "ecobot.service" in irrigation["Before"]
    assert irrigation["User"] == ["ecobot"] and irrigation["StateDirectory"] == ["ecobot"]          # (the folder and its owner the others')
    by_env = {f.metadata["env"]: old_names(f) for f in fields(Config) if f.metadata.get("env")}
    for name in ("BOT_TELEGRAM_TOKEN", "BOT_OPENROUTER_API_KEY", "MCP_ECOWITT_API_KEY", "MCP_ECOWITT_APP_KEY", "MCP_AIRGRADIENT_API_TOKEN",
                 "MCP_HOMEASSISTANT_TOKEN"):
        assert {name, *by_env[name]} <= set(irrigation["UnsetEnvironment"]), name
    assert not any(n.startswith("IRRIGATION_") or n == "AUTH_TOKENS" for n in irrigation["UnsetEnvironment"])
    assert "WantedBy=multi-user.target" in text
    from irrigation_mcp.__main__ import SETTINGS_EXIT          # (without its settings it isn't restarted every 10 s for good)
    assert irrigation["Restart"] == ["on-failure"] and irrigation["RestartPreventExitStatus"] == [str(SETTINGS_EXIT)]


def test_the_bots_unit_wants_irrigation_mcps_only_as_a_wish_and_drops_the_tuya_secrets():
    """The bot's unit wants irrigation-mcp's and starts after it (Wants=: a unit that isn't installed changes nothing, so the bot starts
    without it), and never sees the controller's secrets, by any of their names; ecobot-mcp's starts before irrigation-mcp's."""
    _, bot = unit("ecobot.service")
    assert "irrigation-mcp.service" in bot["Wants"] and "irrigation-mcp.service" in bot["After"]
    assert not re.search(r"^(Requires|BindsTo|Requisite)=.*irrigation", (ROOT / "ecobot.service").read_text(), re.M)
    by_env = {f.metadata["env"]: old_names(f) for f in fields(Config) if f.metadata.get("env")}
    for name in ("IRRIGATION_TUYA_CLIENT_ID", "IRRIGATION_TUYA_CLIENT_SECRET"):
        assert {name, *by_env[name]} <= set(bot["UnsetEnvironment"]), name
    assert "IRRIGATION_TUYA_DEVICE_ID" not in bot["UnsetEnvironment"]   # (kept: the bot warns when it is set and BOT_IRRIGATION_MCP_URL isn't)
    assert "irrigation-mcp.service" in unit("ecobot-mcp.service")[1]["Before"]


def test_docker_leaves_the_bot_without_irrigation_unless_set_and_every_compose_file_covers_irrigation_mcp():
    bot_env = (ROOT / "docker" / "bot.env").read_text()
    assert re.search(r"^BOT_IRRIGATION_MCP_URL=$", bot_env, re.M) and "http://irrigation-mcp:8766/mcp" in bot_env   # (empty; how to set it)
    host = (ROOT / "compose.host-network.yaml").read_text()
    irrigation = host.split("  irrigation-mcp:\n")[1]
    assert "network_mode: host" in irrigation and "ports: !reset []" in irrigation
    assert "IRRIGATION_ECOBOT_MCP_URL: http://127.0.0.1:8765/mcp" in irrigation and "BOT_IRRIGATION_MCP_URL:" not in host
    journald = (ROOT / "compose.journald.yaml").read_text()
    assert all(f"  {name}:\n    logging: !override\n      driver: journald" in journald for name in ("ecobot", "ecobot-mcp", "irrigation-mcp"))
    services = dict(re.findall(r"^  ([\w-]+):\n((?:    .*\n|\s*\n)+)", (ROOT / "compose.yaml").read_text(), re.M))
    irrigation = services["irrigation-mcp"]
    assert "depends_on: [ecobot-mcp]" in irrigation and "- /var/lib/private/ecobot:/data" in irrigation
    assert "env_file: [docker/irrigation.env, /etc/ecobot.env]" in irrigation   # (/etc/ecobot.env after: its settings win, as the bot's)


def test_the_readme_installs_and_upgrades_irrigation_mcp_for_systemd_and_docker():
    readme = (ROOT / "README.md").read_text()
    irrigation = readme.split("### Irrigation\n", 1)[1].split("\n## ", 1)[0]
    assert "BOT_IRRIGATION_MCP_URL=http://127.0.0.1:8766/mcp" in irrigation and "BOT_IRRIGATION_MCP_URL=http://irrigation-mcp:8766/mcp" in irrigation
    assert "sudo cp /opt/ecobot/irrigation-mcp.service /etc/systemd/system/" in irrigation and "COMPOSE_PROFILES=bot,irrigation" in irrigation
    assert "sudo systemctl enable --now irrigation-mcp.service" in irrigation
    assert all(f"IRRIGATION_TUYA_{n}=" in irrigation for n in ("DEVICE_ID", "CLIENT_ID", "CLIENT_SECRET"))
    upgrade = readme.split("## Upgrading", 1)[1].split("\n## ", 1)[0]
    systemd, docker = upgrade.split("### systemd", 1)[1].split("### Docker", 1)
    assert "irrigation-mcp.service" in systemd and "From a bot that ran the irrigation controller itself" in systemd
    assert "From a bot that ran the irrigation controller itself" in docker and "http://irrigation-mcp:8766/mcp" in docker


def test_no_workflow_has_a_plain_value_yaml_would_read_as_a_mapping():
    # A plain (unquoted) value with ": " in it, or ending in ":", is a YAML error, and GitHub then fails the workflow at once with no
    # job (docker.yml's "Charts drawn … (the bot's draws none: it has no Pillow)" did). PyYAML isn't a dependency, so this reads the
    # one-line `key: value` forms the workflows use, leaving block scalars (`run: |`) and quoted or flow values alone.
    bad = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            m = re.match(r"^\s*(?:- )?[\w-]+: (.+)$", line)
            if not m:
                continue
            value = re.sub(r"\s+#.*$", "", m.group(1)).strip()
            if not value or value[0] in "\"'[{|>&*!":
                continue
            if ": " in value or value.endswith(":"):
                bad.append(f"{path.name}:{n}: {line.strip()}")
    assert not bad, "quote these values:\n" + "\n".join(bad)
