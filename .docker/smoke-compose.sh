#!/usr/bin/env bash
set -euo pipefail
echo ">> docker compose config validates"
docker compose --env-file .env.example config >/dev/null
echo "OK compose config valid"
