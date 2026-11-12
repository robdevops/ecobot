# Three images from one Dockerfile, each python:3.13-slim with its runtime packages in a venv that has no pip, no pip cache and no test
# suites, and bytecode for only what its program imports at start (bytecode for everything is 48 MB more; none adds ~2 s to every start):
#   ecobot-mcp (target ecobot-mcp): python -m ecobot_mcp, the sources, the alert rules and the MCP server; requirements-mcp.txt and
#     Pillow (requirements-charts.txt), no Telegram and no model client;
#   ecobot (target ecobot, the default): the Telegram bot, python ecobot.py, a client of ecobot-mcp (BOT_MCP_URL); requirements-bot.txt
#     alone (Telegram, the model's client, the MCP client, httpx): it draws nothing and runs no source, so no Pillow and no source's
#     packages (docs/ecobot-mcp.md step 6b).
#   irrigation-mcp (target irrigation-mcp): python -m irrigation_mcp, the irrigation controller's MCP server, its watchdog and daily
#     check (docs/ecobot-mcp.md 3.9); requirements-irrigation.txt alone: no Telegram, no model client, no Pillow, no source.
#   docker build --target ecobot-mcp -t ecobot-mcp .
#   docker build --target irrigation-mcp -t irrigation-mcp .
#   docker build --target ecobot -t ecobot .
#   docker compose up -d --build   (compose.yaml builds both)
# Settings come from the environment, as under systemd (README); the state and caches are kept in /data, the folder mounted from the host.
FROM python:3.13-slim AS base

FROM base AS fonts
# Inter for the charts, in the three weights they use (text, semibold titles, bold labels); the rest of the package stays here
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-inter \
 && mkdir /fonts \
 && find /usr/share/fonts -name 'Inter-Regular.*' -o -name 'Inter-SemiBold.*' -o -name 'Inter-Bold.*' | xargs cp -t /fonts

# The base image's pip installs into a venv made without one
FROM base AS venv-mcp
COPY requirements-mcp.txt requirements-charts.txt /tmp/
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --no-cache-dir --no-compile --root-user-action=ignore -r /tmp/requirements-mcp.txt -r /tmp/requirements-charts.txt \
 && find /opt/venv -type d -name tests -prune -exec rm -rf {} +

FROM base AS venv-bot
COPY requirements-bot.txt /tmp/
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --no-cache-dir --no-compile --root-user-action=ignore -r /tmp/requirements-bot.txt \
 && find /opt/venv -type d -name tests -prune -exec rm -rf {} +

FROM base AS venv-irrigation
COPY requirements-irrigation.txt /tmp/
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --no-cache-dir --no-compile --root-user-action=ignore -r /tmp/requirements-irrigation.txt \
 && find /opt/venv -type d -name tests -prune -exec rm -rf {} +

# What they all run: the user, the fonts, the shared code. The program reads the running commit for its start-up log line from .git (HEAD,
# the branch's ref, packed-refs: .dockerignore lets only these through), so building from a clone needs nothing more; COMMIT, if given,
# is used instead (a build from files that are not a clone).
FROM base AS app
COPY --from=fonts /fonts/ /usr/share/fonts/inter/
RUN groupadd --system --gid 10001 ecobot \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /data ecobot \
 && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY ecobot_mcp ecobot_mcp
COPY irrigation_mcp irrigation_mcp
COPY lib lib
COPY schemas schemas
COPY scripts scripts
COPY .git* .git/
ARG COMMIT=
RUN if [ -n "$COMMIT" ]; then echo "$COMMIT" > .git/HEAD; fi
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1

# ecobot-mcp. Importing it once writes the bytecode of every module it loads at start, and only those, and checks it needs nothing
# but its own packages (no Telegram, no model client: they are not in this venv)
FROM app AS ecobot-mcp
COPY --from=venv-mcp /opt/venv /opt/venv
RUN DATA_DIR=/tmp/build python -c "import sys, importlib.util; import ecobot_mcp.__main__, lib.mcp_server; \
assert not {'telegram', 'openai'} & set(sys.modules), 'ecobot-mcp imported the bot'; \
assert importlib.util.find_spec('telegram') is None and importlib.util.find_spec('openai') is None" \
 && rm -rf /tmp/build
ENV DATA_DIR=/data
USER 10001:10001
CMD ["python", "-m", "ecobot_mcp"]

# irrigation-mcp. The same: the bytecode of what it loads at start, and a check that it needs nothing but its own packages and imports no
# Telegram, no model client, no Pillow and no source
FROM app AS irrigation-mcp
COPY --from=venv-irrigation /opt/venv /opt/venv
RUN DATA_DIR=/tmp/build python -c "import sys, importlib.util; import irrigation_mcp.__main__, lib.irrigation.server, lib.irrigation.check, \
lib.irrigation.ecobot, lib.irrigation.state, lib.alert_log; \
assert not [m for m in sys.modules if m.split('.')[0] in ('telegram', 'openai', 'PIL', 'ecobot_mcp') or m.startswith(('lib.ecowitt', \
'lib.airgradient', 'lib.forecast', 'lib.pollen', 'lib.storage', 'lib.service', 'lib.mcp_server', 'lib.bot'))], 'irrigation-mcp imported another program'; \
assert importlib.util.find_spec('telegram') is None and importlib.util.find_spec('PIL') is None" \
 && rm -rf /tmp/build
ENV DATA_DIR=/data
USER 10001:10001
CMD ["python", "-m", "irrigation_mcp"]

# The bot (the default target). The same: the bytecode of what it loads at start, and a check that it needs nothing but its own packages
# and imports no source and no drawing (no Pillow: it is not in this venv)
FROM app AS ecobot
COPY --from=venv-bot /opt/venv /opt/venv
COPY ecobot.py ./
RUN DATA_DIR=/tmp/build python -c "import sys, importlib.util; import lib.bot, ecobot, lib.upstream, lib.weather, lib.irrigation.check; \
assert not [m for m in sys.modules if m.split('.')[0] == 'PIL' or m.startswith(('lib.ecowitt', 'lib.airgradient', 'lib.forecast', \
'lib.pollen', 'lib.storage', 'lib.charts', 'lib.draw', 'lib.gauges'))], 'the bot imported a source or the drawing'; \
assert importlib.util.find_spec('PIL') is None" \
 && rm -rf /tmp/build
ENV DATA_DIR=/data
USER 10001:10001
CMD ["python", "ecobot.py"]
