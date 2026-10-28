#!/usr/bin/env bash
# Publica o cierra un issue de aviso en GitHub (necesita GH_TOKEN y GH_REPO; solo se usa en Actions).
# Al publicar, envía además el mismo texto por Telegram si hay TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
# (con TELEGRAM_SOLO_LUNES=1, solo si hoy es lunes: para avisos que no urgen a diario).
#   notify.sh open  "<título>" <fichero-con-el-cuerpo>   crea el issue, o actualiza el que ya esté abierto
#   notify.sh telegram "" <fichero>                      solo envía el fichero por Telegram (para probar el bot)
#   notify.sh close "<título>"                           cierra el issue abierto con ese título, si lo hay
set -euo pipefail

action=$1
title=$2

telegram() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
  [ -z "${TELEGRAM_SOLO_LUNES:-}" ] || [ "$(date +%u)" = 1 ] || return 0
  # Devuelve error si falla; al publicar un aviso se ignora, porque el issue ya está publicado.
  curl -fsS --max-time 30 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text@$1" > /dev/null \
    || { echo "::warning::No se pudo enviar el aviso por Telegram"; return 1; }
}

if [ "$action" = telegram ]; then  # solo Telegram, sin tocar issues (prueba manual)
  telegram "$3"
  exit 0
fi

number=$(gh issue list --state open --limit 100 --json number,title \
  --jq "[.[] | select(.title == \"$title\")][0].number // empty")

case "$action" in
  open)
    if [ -n "$number" ]; then
      gh issue edit "$number" --body-file "$3"
    else
      gh issue create --title "$title" --body-file "$3"
    fi
    telegram "$3" || true
    ;;
  close)
    if [ -n "$number" ]; then
      gh issue close "$number" --comment "Resuelto: la última ejecución terminó sin avisos."
    fi
    ;;
  *)
    echo "uso: notify.sh open|close <título> [fichero]" >&2
    exit 2
    ;;
esac
