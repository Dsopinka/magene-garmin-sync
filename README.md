# magene-garmin-sync

Gets your **Magene** rides into **Garmin Connect** by way of **Strava**.

Your Magene head unit (e.g. C606 Pro) already syncs rides to Strava via the
OnelapFit app. This tool reads each new ride from Strava through Strava's
official API — which you authorize — rebuilds it with its GPS, heart rate, power
and cadence, and uploads it to Garmin Connect. It runs headless as a Docker
container with a small web control panel, built for an Unraid server.

It only reads your own Strava activities and writes to your own Garmin account,
using your own credentials.

## Why via Strava

Onelap's cloud API is walled off at the network edge (it only accepts the phone
app), so a server can't read it directly. Strava is the clean, supported path:
the rides are already there, and Strava has a real API for your own data.

Reconstructed rides carry all the measurements (GPS, HR, power, cadence,
altitude), but they aren't the byte-identical Magene original. Garmin may show
them as imported activities; depending on Garmin's rules, imported non-Garmin
activities don't always count toward Training Status.

## How it works

1. Reads your recent activities from Strava.
2. Keeps the ones you want (cycling only by default; optional device-name filter
   like `Magene`; virtual/Zwift rides off by default).
3. For each new ride, pulls its streams and builds a `.tcx` file.
4. Uploads to Garmin Connect; remembers what's synced (`/data/sync_state.json`).
5. Repeats on a schedule.

## Setup

### 1. Create a Strava API application (one time, free)

- Go to <https://www.strava.com/settings/api> and create an app.
- Set **Authorization Callback Domain** to your server's address, e.g.
  `192.168.68.117` (the Unraid IP you open the panel at — no `http://`, no port).
- Note the **Client ID** and **Client Secret**.

### 2. Open the panel and connect

- Open `http://<your-server>:8787`.
- Enter the Strava **Client ID** and **Client Secret**, enter your **Garmin**
  email/password, click **Save settings**.
- Click **Connect Strava** → approve on Strava → you're redirected back and it
  shows **connected**.
- (Optional) set a **Device filter** like `Magene` to sync only Magene rides,
  and leave "Cycling only" on. Save.
- Click **Test Strava** to see your recent rides and which would sync, then
  **Sync now**. Turn on **Auto-sync** to keep it running.

The panel shows the exact callback address to register, at the bottom.

## Deploy on Unraid

In the Unraid web terminal (`>_`):

```
mkdir -p /mnt/user/appdata/magene-garmin-sync && cd /mnt/user/appdata/magene-garmin-sync
curl -fsSL https://github.com/Dsopinka/magene-garmin-sync/archive/refs/heads/main.tar.gz | tar xz --strip-components=1
docker build -t magene-garmin-sync:0.1.0 .
docker rm -f magene-garmin-sync 2>/dev/null
docker run -d --name magene-garmin-sync -p 8787:8787 \
  -v /mnt/user/appdata/magene-garmin-sync/data:/data \
  --restart unless-stopped magene-garmin-sync:0.1.0
```

Then open `http://<unraid-ip>:8787`. The `/data` folder holds your settings,
Strava/Garmin tokens, and sync state across rebuilds.

## Environment variables (all optional — the UI covers everything)

| Variable | Notes |
| --- | --- |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | pre-fill the Strava app creds |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | pre-fill Garmin login |
| `GARMIN_MFA` | 2FA code for a headless Garmin re-login |
| `DEVICE_FILTER` | e.g. `Magene` |
| `POLL_SECONDS` | sync interval (default 3600) |
| `SCAN_COUNT` | recent activities scanned per cycle (default 30) |
| `TZ` | your timezone |

## Notes

- Garmin uploads use the maintained
  [`python-garminconnect`](https://pypi.org/project/garminconnect/) library.
- Keep the panel on your LAN; it's an unauthenticated control panel for your own
  accounts. Don't expose port 8787 to the internet.
- For your own data and accounts only.
