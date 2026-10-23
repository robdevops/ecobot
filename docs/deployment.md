# Running it for real

Three ways to run the bot, from simplest to most locked down. Settings are in [configuration.md](configuration.md).

## By hand

Handy for trying it out. In a clone you own (not `/opt/ecobot`, which is root's) with `/etc/ecobot.env` loaded as in [configuration.md](configuration.md).

Python 3.13.5 (see `.python-version`; the code uses 3.12 and 3.13 features, so older Pythons will not run it):

```
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ecobot.py          # with /etc/ecobot.env loaded, see configuration.md
```

## systemd

Under systemd see `ecobot.service`. The Git repo is checked out to `/opt/ecobot`, outside any home directory, and the unit runs it sandboxed as the `ecobot` user (uid 10001, the same as the Docker image's) with its state in `/var/lib/ecobot` (it loads the environment file; the checkout's location is its `WorkingDirectory` and `ExecStart` lines).

The unit file's header has the install steps and how to move an older install's state into `/var/lib/ecobot`.

## Docker

In Docker, the `Dockerfile` builds a python:3.13-slim image (278 MB unpacked, 98 MB compressed):

```
docker build --build-arg COMMIT=$(git rev-parse HEAD) -t ecobot .   # COMMIT: the commit the start-up log names
docker run -d --name ecobot --restart unless-stopped --env-file /etc/ecobot.env -v /var/lib/ecobot:/data ecobot
```
The state and caches go in `/data` (its `DATA_DIR`), here the same `/var/lib/ecobot` the systemd unit uses, which belongs to uid 10001 in both, so you can switch between systemd and Docker with nothing to migrate (run only one at a time). If the folder does not exist yet, create it with `sudo install -d -o 10001 -g 10001 /var/lib/ecobot`; any other host folder must be writable by uid 10001 too. Docker reads the environment file's `KEY=value` lines as they are: a value in quotes keeps its quotes, where systemd would drop them.
