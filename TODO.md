# To do

Things to check or finish that need something this repo can't do on its own.

## Check when PM2.5 is high

Does the sensor's `pm02Compensated` equal the cloud's `pm02_corrected`? Both read 0 on the day it was tried, so it is not known. The bot records
`pm02Compensated` (preferring it to the plain `pm02`) as its PM2.5. If the two differ, a chart would show a step wherever the bot's own readings
meet the cloud's.

When the air has real particles in it (smoke, incense, a dusty day), run these within a minute of each other, with `TOKEN` set to the
`AIRGRADIENT_API_TOKEN` and the sensor's address in place of the name if `.local` does not resolve:

```
curl -s "https://api.airgradient.com/public/api/v1/locations/153523/measures/current?token=$TOKEN" | jq '{timestamp, pm02, pm02_corrected}'
curl -s http://airgradient_d83bda1c1e08.local/measures/current | jq '{pm02, pm02Compensated}'
```

Equal (or within a few percent): nothing to do. Different: compare over a longer stretch with the `measures/past` call (5-minute rows, stamped with the start of
the interval) before deciding which to trust.

## Try on the real machines

- The sensor's own server: `AIRGRADIENT_LOCAL_URL` on a bot on the sensor's network. Check the log says "live readings from the sensor itself", that "air now" is
  fresh, and that a chart of today has no step where the bot's readings begin.
- Without a cloud account (no `AIRGRADIENT_API_TOKEN`): nothing should be asked of the cloud, and the first charts only reach back to when the bot began recording.
- `ecobot.service` as a real unit (`MemoryDenyWriteExecute`, `AF_NETLINK` for finding the sensor by name, `StateDirectory`, `CacheDirectory`): start it, then ask for a chart, an
  analysis question and something that reads the caches.
- The Docker image with `zeroconf` in it: the `image` check on a pull request builds it.
- `ALERT_COOLDOWN_MINUTES` in the production env file: the default is now 60; a line setting it to 30 keeps 30.
