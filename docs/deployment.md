# Running it for real

Three ways to run the bot, from simplest to most locked down. Settings are in [configuration.md](configuration.md).

## By hand

Python 3.13.5 (see `.python-version`; the code uses 3.12 and 3.13 features, so older Pythons will not run it):

```
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ecobot.py          # with /etc/ecobot.env loaded, see configuration.md
```

## systemd

Under systemd see `ecobot.service`. The Git repo is checked out to `/opt/ecobot`, outside any home directory, and the unit runs it sandboxed as a dynamic user with its state in `/var/lib/ecobot` (it loads the environment file; the checkout's location is its `WorkingDirectory` and `ExecStart` lines).

The unit file's header has the install steps and how to move an older install's state into `/var/lib/ecobot`.

## Docker

In Docker, the `Dockerfile` builds a python:3.13-slim image (278 MB unpacked, 98 MB compressed):

```
docker build --build-arg COMMIT=$(git rev-parse HEAD) -t ecobot .   # COMMIT: the commit the start-up log names
docker run -d --name ecobot --restart unless-stopped --env-file /etc/ecobot.env -v ecobot-data:/data ecobot
```
The state and caches go in `/data` (its `DATA_DIR`), here a named volume. A host folder instead (`-v /srv/ecobot:/data`) must be writable by the image's user, uid 10001 (`sudo chown 10001:10001 /srv/ecobot`). Docker reads the environment file's `KEY=value` lines as they are: a value in quotes keeps its quotes, where systemd would drop them.
