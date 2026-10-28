# The bot as a small image: python:3.13-slim with the runtime packages in a venv that has no pip, no pip cache and no test suites,
# and bytecode for only what the bot imports at start (bytecode for everything is 48 MB more; none adds ~2 s to every start).
#   docker build -t ecobot .
#   docker compose up -d --build   (compose.yaml; LOG_DRIVER from .env, as the README says)
# Settings come from the environment, as under systemd (README); the state and caches are kept in /data, the folder mounted from the host.
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
# The bot reads the running commit for its start-up log line from .git (HEAD, the branch's ref, packed-refs: .dockerignore lets only these
# through), so building from a clone needs nothing more; COMMIT, if given, is used instead (a build from files that are not a clone).
# Importing the bot once writes the bytecode of every module it loads at start, and only those.
COPY .git* .git/
ARG COMMIT=
RUN if [ -n "$COMMIT" ]; then echo "$COMMIT" > .git/HEAD; fi \
 && DATA_DIR=/tmp/build MPLCONFIGDIR=/tmp/build python -c "import lib.bot, ecobot" \
 && rm -rf /tmp/build
ENV DATA_DIR=/data
USER 10001:10001
CMD ["python", "ecobot.py"]
