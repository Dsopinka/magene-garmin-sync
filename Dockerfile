FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY magene_garmin_sync ./magene_garmin_sync

# /data holds the stored config (creds), Garmin token cache, sync state, FITs.
ENV DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8787
VOLUME ["/data"]
EXPOSE 8787

# Default: serve the web control panel. Override with: sync | run | probe | garmin-login
ENTRYPOINT ["python", "-m", "magene_garmin_sync"]
CMD ["serve"]
