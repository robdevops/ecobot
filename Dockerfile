# The bot as a small image: python:3.13-slim with the runtime packages in a venv that has no pip, no pip cache and no test suites,
# and bytecode for only what the bot imports at start (bytecode for everything is 48 MB more; none adds ~2 s to every start).
#   docker build --build-arg COMMIT=$(git rev-parse HEAD) -t ecobot .
#   docker run -d --name ecobot --restart unless-stopped --env-file /etc/ecobot.env -v /var/lib/private/ecobot:/data ecobot
# Settings come from the environment, as under systemd (docs/deployment.md); the state and caches are kept in /data.
FROM python:3.13-slim AS base

FROM base AS build
# Inter for the charts, in the three weights they use (text, semibold titles, bold labels); the rest of the package stays here
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-inter \
 && mkdir /fonts \
 && find /usr/share/fonts -name 'Inter-Regular.*' -o -name 'Inter-SemiBold.*' -o -name 'Inter-Bold.*' | xargs cp -t /fonts
COPY requirements.txt /tmp/
# The base image's pip installs into a venv made without one
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --no-cache-dir --no-compile --root-user-action=ignore -r /tmp/requirements.txt \
 && find /opt/venv -type d -name tests -prune -exec rm -rf {} +

FROM base
COPY --from=build /opt/venv /opt/venv
COPY --from=build /fonts/ /usr/share/fonts/inter/
RUN groupadd --system --gid 10001 ecobot \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /data ecobot \
 && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY ecobot.py ./
COPY lib lib
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
# COMMIT goes where the bot reads the running commit from (.git/HEAD) for its start-up log line ("unknown" without it). Importing
# the bot once writes the bytecode of every module it loads at start, and only those.
ARG COMMIT=unknown
LABEL org.opencontainers.image.revision=$COMMIT
RUN mkdir .git && echo "$COMMIT" > .git/HEAD \
 && DATA_DIR=/tmp/build MPLCONFIGDIR=/tmp/build python -c "import lib.bot, ecobot" \
 && rm -rf /tmp/build
ENV DATA_DIR=/data
USER 10001:10001
CMD ["python", "ecobot.py"]
