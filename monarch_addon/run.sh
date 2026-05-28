#!/usr/bin/with-contenv bashio
# Read credentials from the add-on options (Configuration tab) into the env vars
# server.py expects. No .env file is shipped.
export MONARCH_EMAIL="$(bashio::config 'email')"
export MONARCH_PASSWORD="$(bashio::config 'password')"
export MONARCH_MFA_SECRET="$(bashio::config 'mfa_secret')"

# Bind to all interfaces so Ingress / the published port can reach the app.
export MONARCH_HOST="0.0.0.0"
export MONARCH_PORT="8000"

# Run from /data (the add-on's persistent volume) so the cached login session
# (.mm/mm_session.pickle, written relative to cwd) survives restarts/rebuilds.
cd /data
exec python3 /app/server.py
