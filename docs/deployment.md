# Running it for real

Three ways to run the bot, from simplest to most locked down. Settings are in [configuration.md](configuration.md).

## By hand

For trying it out or watching the log. Use the install from the quick start (`/opt/ecobot`, its `.venv` and `/etc/ecobot.env`), with the service stopped, so there is only one copy of the code and of the data:

```
sudo systemctl stop ecobot
sudo sh -c 'set -a; . /etc/ecobot.env; set +a; cd /opt/ecobot && DATA_DIR=/var/lib/private/ecobot exec .venv/bin/python ecobot.py'
```

Python 3.13.5 (see `.python-version`; the code uses 3.12 and 3.13 features, so older Pythons will not run it). Ctrl-C stops it, and `sudo systemctl start ecobot` hands the data folder back to the service.

## systemd

Under systemd see `ecobot.service`. The Git repo is checked out to `/opt/ecobot`, outside any home directory, and the unit runs it sandboxed as a dynamic user with its state in `/var/lib/ecobot` (it loads the environment file; the checkout's location is its `WorkingDirectory` and `ExecStart` lines).

The unit file's header has the install steps and how to move an older install's state into `/var/lib/ecobot`.

## Docker

In Docker, the `Dockerfile` builds a python:3.13-slim image (278 MB unpacked, 98 MB compressed):

```
docker build --build-arg COMMIT=$(git rev-parse HEAD) -t ecobot .   # COMMIT: the commit the start-up log names
docker run -d --name ecobot --restart unless-stopped --env-file /etc/ecobot.env -v /var/lib/private/ecobot:/data ecobot
```
The state and caches are kept in `/var/lib/private/ecobot` on the host, the folder the systemd unit uses too (`/var/lib/ecobot` links to it), so the two share one set of data. It is mounted at `/data` inside the container, which is the image's `DATA_DIR`. The image runs as uid 10001, so before every `docker run` give it the folder:

```
sudo mkdir -p /var/lib/private/ecobot && sudo chown -R 10001:10001 /var/lib/private/ecobot
```

Run only one of systemd and Docker at a time. Starting the service again takes the folder back for its own user, and the `chown` above hands it to Docker. Any other host folder instead must be writable by uid 10001. Docker reads the environment file's `KEY=value` lines as they are: a value in quotes keeps its quotes, where systemd would drop them.
