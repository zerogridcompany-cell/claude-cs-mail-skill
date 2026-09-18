#!/bin/bash
# Claude専用のブラウザ（＝席）を、遠隔操作口（CDP）つきで起動する。
# 🔴 普段使いのブラウザとは別のプロフィールで動く。本人のブラウザは触らん。
# 使い方: seat.sh [start|stop|status]
#   初回だけ: start → 開いた窓でCSアドレスのGmailに手でログイン（以後は覚えとる）
# 環境変数:
#   CS_SEAT_PORT     既定 9336
#   CS_SEAT_BROWSER  既定 Google Chrome（Dia / Brave / Edge など Chromium系ならOK）
#   CS_SEAT_PROFILE  既定 ~/.config/cs-mail-seat
PORT="${CS_SEAT_PORT:-9336}"
BIN="${CS_SEAT_BROWSER:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PROFILE="${CS_SEAT_PROFILE:-$HOME/.config/cs-mail-seat}"
alive() { curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }
case "${1:-start}" in
  start)
    alive && { echo "すでに起動中 (port $PORT)"; exit 0; }
    mkdir -p "$PROFILE"
    "$BIN" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
      --remote-allow-origins=http://127.0.0.1 --no-first-run --no-default-browser-check \
      "https://mail.google.com/" >/dev/null 2>&1 &
    for i in $(seq 1 20); do alive && { echo "🟢 起動した (port $PORT)"; exit 0; }; sleep 1; done
    echo "🔴 起動できんかった"; exit 1 ;;
  stop)
    pkill -f -- "--remote-debugging-port=$PORT" && echo "止めた" || echo "動いてへん" ;;
  status)
    if alive; then
      n=$(curl -s "http://127.0.0.1:$PORT/json" | /usr/bin/python3 -c "import sys,json;print(len(json.load(sys.stdin)))")
      echo "🟢 起動中 (port $PORT)・タブ ${n} 枚"
      [ "$n" -gt 30 ] && echo "⚠️ タブが溜まっとる。新しいタブが開けず固まる原因。stop→start で直る"
    else echo "⚪ 止まっとる"; fi ;;
esac
