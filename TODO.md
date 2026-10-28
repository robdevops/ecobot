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

## Try Home Assistant as the weather station's source, then redesign the storage

Set `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` (and leave `ECOWITT_SOURCE` at `auto`, or try `homeassistant` with no Ecowitt keys) and run it for a week or two.
It has only been run against a fake Home Assistant. Check in the log and the charts:
- the start-up line says where the readings come from, and the live poll and minute pulls don't warn (or warn once and recover);
- a chart of a few hours is drawn a reading a minute, a day or a week looks right, and a chart past Home Assistant's 10 days still has its bands (from the cache);
- a rain alert comes within seconds of the gauge's report; "weather now" is fresh;
- with both set, nothing is asked of the cloud except what Home Assistant lacks.

Then the storage redesign: [docs/storage-plan.md](docs/storage-plan.md) has the measurements, the design, the migration and the steps (the
requirements that were here are its section 2). It starts with three decisions (its section 6.1) and step 0, measuring the real cache.
