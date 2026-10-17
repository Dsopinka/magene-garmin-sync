# magene-garmin-sync

Pulls your rides from the **Magene / OnelapFit** cloud and uploads them to
**Garmin Connect**, so rides recorded on a Magene head unit (e.g. C606 Pro) show
up in Garmin Connect and count toward your training metrics. Runs headless as a
Docker container — built for an Unraid server.

It only ever reads from your Onelap account and writes to your Garmin account.
It uses your own credentials, and never sends them anywhere except the two
services' own login endpoints.

## Why this, and not the heart-rate merge we first discussed

Your Magene FIT files already carry heart rate **and** power from the sensors
paired to the head unit. That HR is better than your watch's optical reading on
changing-weather rides, so there's nothing to "merge in" — the Magene ride is
the good one. This tool just gets that ride into Garmin Connect. You keep
wearing the watch exactly as you do now (it still logs all-day HR, sleep, etc.).

If both your watch and the Magene recorded the same outdoor ride, you'll have
two activities in Garmin Connect. Delete the watch copy if you want; this tool
never deletes anything.

## How it works

1. Sign in to OnelapFit's international backend with your account + password.
2. List your recent ride records; skip any already synced (tracked in
   `/data/sync_state.json`).
3. Download each new ride's FIT file and upload it to Garmin Connect.
4. Sleep, then repeat every `POLL_SECONDS`.

## One-time setup

The Onelap international API has a couple of regional variants. Pin yours down
once with the built-in `probe` command before running the sync.

### 1. Configure

```bash
cp .env.example .env
# edit .env: ONELAP_ACCOUNT/PASSWORD and GARMIN_EMAIL/PASSWORD
```

### 2. Probe Onelap (confirms host + login format, lists 5 recent rides)

```bash
docker compose run --rm magene-garmin-sync probe
```

On success it prints a line like:

```
SUCCESS: host=https://rfs-fitness.rfsvr.com hash_password=False
  set ONELAP_HOST=https://rfs-fitness.rfsvr.com and ONELAP_HASH_PASSWORD=0
```

Copy those two values into `.env`. (Pinning them makes every later run fast and
predictable.) If it lists your recent rides, the Onelap half is working.

> If `probe` can't log in to any candidate host, tell me — the login format may
> need one tweak, which we confirm against one real request. The endpoint paths
> are already correct; only the host/auth envelope is in question.

### 3. Garmin login (caches tokens; handles 2FA once)

```bash
docker compose run --rm magene-garmin-sync garmin-login
```

If your Garmin account uses 2FA, it prompts for the code. Tokens are cached in
`/data/garmin_tokens` and auto-refresh for ~a year, so the loop won't need the
code again.

### 4. Run it

```bash
docker compose up -d
docker compose logs -f
```

### Try a single pass without the loop

```bash
docker compose run --rm magene-garmin-sync sync
```

## On Unraid

- Add a container (or use "Compose Manager"), image built from this repo.
- Map a volume: `/data` → `/mnt/user/appdata/magene-garmin-sync`.
- Set the `ONELAP_*` and `GARMIN_*` variables in the template.
- Run `probe` and `garmin-login` once from the container console, then start it.

## Environment variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `ONELAP_ACCOUNT` | yes | | Email or phone used for OnelapFit |
| `ONELAP_PASSWORD` | yes | | |
| `ONELAP_HOST` | no | auto | Set from `probe` output |
| `ONELAP_HASH_PASSWORD` | no | `0` | Set from `probe` output |
| `ONELAP_AUTH_SCHEME` | no | empty | e.g. `Bearer ` if required |
| `GARMIN_EMAIL` | yes | | |
| `GARMIN_PASSWORD` | yes | | |
| `GARMIN_MFA` | no | | 2FA code for headless re-login |
| `POLL_SECONDS` | no | `3600` | Sync interval |
| `LOOKBACK_PAGES` | no | `1` | Ride-list pages to scan each cycle |
| `DATA_DIR` | no | `/data` | Token cache, state, FIT files |

## Scope / honesty

- Garmin uploads go through the maintained
  [`python-garminconnect`](https://pypi.org/project/garminconnect/) library,
  which uses Garmin's mobile sign-in flow. That half is solid.
- The Onelap client is an independent implementation against the observed REST
  interface. The endpoint paths are confirmed; the host and auth envelope are
  confirmed per-account by `probe`. If Onelap changes its API, the client may
  need an update.
- This is for your own data and accounts (interoperability). Don't redistribute
  anyone's ride data or account access.
