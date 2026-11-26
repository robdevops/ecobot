"""The programs as they are installed: two Docker images from one Dockerfile (ecobot-mcp's with Pillow; irrigation-mcp's with its
own packages alone, no Pillow and no source, docs/ecobot-mcp.md step 8), compose running each from its own, and the systemd units,
which drop each other's secrets (and the old Telegram bot's) by their new and old names. (Read as text: the CI workflow docker.yml
builds and runs the images themselves.)"""

import json
import os
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


def test_each_program_needs_only_its_own_packages():
    mcp, irrigation = requirements("requirements-mcp.txt"), requirements("requirements-irrigation.txt")
    both = requirements("requirements.txt")
    assert {"mcp", "uvicorn", "httpx", "holidays", "zeroconf"} <= mcp and not {"python-telegram-bot", "openai"} & mcp
    assert irrigation == {"mcp", "uvicorn", "httpx"}                           # irrigation-mcp: the server, its client, Tuya's client
    assert both == mcp | irrigation                                            # (a systemd install's one venv: both programs)
    assert not (ROOT / "requirements-bot.txt").exists() and "pillow" in requirements("requirements-charts.txt")
    pins = lambda name: {line.split("#")[0].strip() for line in (ROOT / name).read_text().splitlines() if line.startswith(("mcp", "uvicorn"))}
    assert pins("requirements-irrigation.txt") == pins("requirements-mcp.txt")   # the sides of MCP upgraded together


def test_one_dockerfile_builds_both_images_each_checked_at_build():
    text = (ROOT / "Dockerfile").read_text()
    assert not re.search(r"^FROM app AS ecobot$", text, re.M) and "venv-bot" not in text and "ecobot.py" not in text
    stages = re.findall(r"^FROM \S+ AS (\S+)$", text, re.M)
    assert stages[-1] == "ecobot-mcp" and {"ecobot-mcp", "irrigation-mcp"} <= set(stages)   # (a bare docker build makes ecobot-mcp)
    mcp = text.split("FROM app AS ecobot-mcp")[1]
    irrigation = text.split("FROM app AS irrigation-mcp")[1].split("FROM app AS ecobot-mcp")[0]
    venv_mcp = text.split("FROM base AS venv-mcp")[1].split("FROM base AS venv-irrigation")[0]
    venv_irrigation = text.split("FROM base AS venv-irrigation")[1].split("FROM base AS app")[0]
    assert "-r /tmp/requirements-mcp.txt" in venv_mcp and "-r /tmp/requirements-charts.txt" in venv_mcp
    assert "-r /tmp/requirements-irrigation.txt" in venv_irrigation and "requirements-charts" not in venv_irrigation
    assert 'CMD ["python", "-m", "ecobot_mcp"]' in mcp and "import ecobot_mcp.__main__" in mcp and "find_spec('telegram') is None" in mcp
    assert 'CMD ["python", "-m", "irrigation_mcp"]' in irrigation and "import irrigation_mcp.__main__" in irrigation
    assert "find_spec('telegram') is None" in irrigation and "find_spec('PIL') is None" in irrigation and "'lib.storage'" in irrigation
    ignored = (ROOT / ".dockerignore").read_text()
    assert all(f"!{name}" in ignored for name in ("requirements-mcp.txt", "requirements-irrigation.txt", "irrigation_mcp/", "ecobot_mcp/"))
    assert "!ecobot.py" not in ignored and "requirements-bot" not in ignored


def test_compose_runs_each_program_from_its_own_image():
    text = (ROOT / "compose.yaml").read_text()
    services = dict(re.findall(r"^  ([\w-]+):\n((?:    .*\n|\s*\n)+)", text, re.M))
    assert set(services) == {"ecobot-mcp", "irrigation-mcp"}
    mcp, irrigation = services["ecobot-mcp"], services["irrigation-mcp"]
    assert "image: irrigation-mcp" in irrigation and "target: irrigation-mcp" in irrigation and "profiles: [irrigation]" in irrigation
    assert "IRRIGATION_BIND: 0.0.0.0:8766" in irrigation and "- 8766:8766" in irrigation and "docker/irrigation.env" in irrigation
    assert "IRRIGATION_ECOBOT_MCP_URL=http://ecobot-mcp:8765/mcp" in (ROOT / "docker" / "irrigation.env").read_text()
    assert "image: ecobot-mcp" in mcp and "target: ecobot-mcp" in mcp and "hostname: ecobot-mcp" in mcp and "- 8765:8765" in mcp
    assert "profiles" not in mcp and not (ROOT / "docker" / "bot.env").exists()


def test_the_ecobot_mcp_image_compiles_what_its_start_and_first_requests_import(tmp_path):
    """The image's user can't write bytecode, so what the build step doesn't import is compiled again at every start: the tools
    (lib.service.*), uvicorn's HTTP protocol and lifespan, anyio's asyncio backend, the clients' connection pool and certificates. The
    step itself is run here (bar its check that Telegram and the model client are not installed)."""
    import subprocess
    import sys
    text = (ROOT / "Dockerfile").read_text().split("FROM app AS ecobot-mcp")[1]
    program = re.search(r'RUN DATA_DIR=/tmp/build python -c "(.*?)" \\\n', text, re.S)[1].replace("\\\n", "")
    program = program.split("; assert importlib.util.find_spec")[0]
    shown = "import json; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", f"{program}; {shown}"], cwd=ROOT, env={**os.environ, "DATA_DIR": str(tmp_path)},
                         capture_output=True, text=True, check=True).stdout
    loaded = set(json.loads(out.splitlines()[-1]))
    service = {f"lib.service.{p.stem}" for p in (ROOT / "lib" / "service").glob("*.py") if p.stem != "__init__"}
    assert service <= loaded
    assert {"uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on", "anyio._backends._asyncio", "httpcore", "truststore", "certifi",
            "ecobot_mcp.__main__", "lib.mcp_server"} <= loaded
    assert not {"telegram", "openai"} & loaded


def test_the_ecobot_mcp_unit_drops_the_bots_secrets_by_their_new_and_old_names():
    text = (ROOT / "ecobot-mcp.service").read_text()
    unset = set(re.search(r"^UnsetEnvironment=(.*)$", text, re.M)[1].split())
    by_env = {f.metadata["env"]: old_names(f) for f in fields(Config) if f.metadata.get("env")}
    for name in ("BOT_TELEGRAM_TOKEN", "BOT_OPENROUTER_API_KEY", "IRRIGATION_TUYA_CLIENT_ID", "IRRIGATION_TUYA_CLIENT_SECRET"):
        assert {name, *by_env.get(name, ())} <= unset, name   # (the bot's settings are no Config fields any more: named in the unit only)
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


def unit(name: str) -> tuple[str, dict[str, list[str]]]:
    text = (ROOT / name).read_text()
    keys = ("Wants", "After", "Before", "UnsetEnvironment", "ExecStart", "User", "StateDirectory", "Restart", "RestartPreventExitStatus")
    return text, {k: (m[1].split() if (m := re.search(rf"^{k}=(.*)$", text, re.M)) else []) for k in keys}


def test_irrigation_mcps_unit_runs_it_after_ecobot_mcp_without_the_others_secrets():
    """Step 8b: irrigation-mcp's own unit, from the same checkout and venv, the same dynamic user and data folder; it wants ecobot-mcp's
    (its daily check asks it) and starts after it; it drops ecobot-mcp's secrets and the old Telegram bot's, by every name they have had."""
    text, irrigation = unit("irrigation-mcp.service")
    assert irrigation["ExecStart"] == ["/opt/ecobot/.venv/bin/python", "-m", "irrigation_mcp"]
    assert "ecobot-mcp.service" in irrigation["Wants"] and "ecobot-mcp.service" in irrigation["After"] and not irrigation["Before"]
    assert unit("ecobot-mcp.service")[1]["Before"] == ["irrigation-mcp.service"]
    assert irrigation["User"] == ["ecobot"] and irrigation["StateDirectory"] == ["ecobot"]          # (the folder and its owner the others')
    by_env = {f.metadata["env"]: old_names(f) for f in fields(Config) if f.metadata.get("env")}
    for name in ("BOT_TELEGRAM_TOKEN", "BOT_OPENROUTER_API_KEY", "MCP_ECOWITT_API_KEY", "MCP_ECOWITT_APP_KEY", "MCP_AIRGRADIENT_API_TOKEN",
                 "MCP_HOMEASSISTANT_TOKEN"):
        assert {name, *by_env.get(name, ())} <= set(irrigation["UnsetEnvironment"]), name   # (the old bot's: no Config fields any more)
    assert not any(n.startswith("IRRIGATION_") or n == "AUTH_TOKENS" for n in irrigation["UnsetEnvironment"])
    assert "WantedBy=multi-user.target" in text
    from irrigation_mcp.__main__ import SETTINGS_EXIT          # (without its settings it isn't restarted every 10 s for good)
    assert irrigation["Restart"] == ["on-failure"] and irrigation["RestartPreventExitStatus"] == [str(SETTINGS_EXIT)]


def test_every_compose_file_covers_only_the_two_services():
    host = (ROOT / "compose.host-network.yaml").read_text()
    irrigation = host.split("  irrigation-mcp:\n")[1]
    assert "network_mode: host" in irrigation and "ports: !reset []" in irrigation
    assert "IRRIGATION_ECOBOT_MCP_URL: http://127.0.0.1:8765/mcp" in irrigation
    assert re.findall(r"^  ([\w-]+):$", host, re.M) == ["ecobot-mcp", "irrigation-mcp"]
    journald = (ROOT / "compose.journald.yaml").read_text()
    assert re.findall(r"^  ([\w-]+):$", journald, re.M) == ["ecobot-mcp", "irrigation-mcp"]
    assert all(f"  {name}:\n    logging: !override\n      driver: journald" in journald for name in ("ecobot-mcp", "irrigation-mcp"))
    services = dict(re.findall(r"^  ([\w-]+):\n((?:    .*\n|\s*\n)+)", (ROOT / "compose.yaml").read_text(), re.M))
    irrigation = services["irrigation-mcp"]
    assert "depends_on: [ecobot-mcp]" in irrigation and "- /var/lib/private/ecobot:/data" in irrigation
    assert "env_file: [docker/irrigation.env, /etc/ecobot.env]" in irrigation   # (/etc/ecobot.env after: its settings win)


def test_the_packaging_names_no_bot_program():
    assert not (ROOT / "ecobot.service").exists()
    for path in ("ecobot-mcp.service", "irrigation-mcp.service"):
        text = (ROOT / path).read_text()
        assert not re.search(r"^(Before|After|Wants|Requires)=.*\becobot\.service", text, re.M), path
    workflows = "".join(p.read_text() for p in (ROOT / ".github" / "workflows").glob("*.yml"))
    assert "--target ecobot " not in workflows and "requirements-bot" not in workflows and "eval_prompts" not in workflows
    assert "NOTIFY_TELEGRAM" not in workflows


def test_no_workflow_has_a_plain_value_yaml_would_read_as_a_mapping():
    # A plain (unquoted) value with ": " in it, or ending in ":", is a YAML error, and GitHub then fails the workflow at once with no
    # job (docker.yml's "Charts drawn … (irrigation-mcp's draws none: it has no Pillow)" did). PyYAML isn't a dependency, so this reads the
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
