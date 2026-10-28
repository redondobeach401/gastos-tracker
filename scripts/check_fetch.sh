#!/usr/bin/env bash
# Avisa si fetch-bank no ha terminado bien en las últimas MAX_HOURS horas (36 por defecto), p. ej. porque
# cron-job.org ha dejado de lanzarlo. Necesita GH_TOKEN y GH_REPO; solo se usa en Actions.
set -euo pipefail

title="enable-banking: el fetch no se ha ejecutado"
max_hours=${MAX_HOURS:-36}

last=$(gh run list --workflow fetch-bank.yml --status success --limit 1 --json createdAt \
  --jq '.[0].createdAt // empty')

if [ -n "$last" ]; then
  age_hours=$(( ($(date +%s) - $(date -d "$last" +%s)) / 3600 ))
  if [ "$age_hours" -le "$max_hours" ]; then
    echo "Último fetch correcto: $last (hace ${age_hours} h)"
    bash "$(dirname "$0")/notify.sh" close "$title"
    exit 0
  fi
  detalle="El último fetch correcto fue el $last (hace ${age_hours} h)."
else
  detalle="No hay ningún fetch correcto en el historial de ejecuciones."
fi

printf '## %s\n\n%s Comprueba que cron-job.org sigue lanzando el workflow (cuenta, token de GitHub caducado) '\
'y mira las ejecuciones de fetch-bank en Actions.\n' "$title" "$detalle" > fetch-parado.md
cat fetch-parado.md
bash "$(dirname "$0")/notify.sh" open "$title" fetch-parado.md
