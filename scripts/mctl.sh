#!/usr/bin/env bash
# メンテナンスモード / Lambda 差し替え操作ツール (bash / Git Bash / WSL / macOS / Linux)
#   ./scripts/mctl.sh status
#   ./scripts/mctl.sh on  intraweb
#   ./scripts/mctl.sh off all
#   ./scripts/mctl.sh rules sfapi
#   ./scripts/mctl.sh lambda                    # 現在の Lambda 実装 (variant) を表示
#   ./scripts/mctl.sh lambda custom             # 全 ALB を自作 Lambda へ差し替え
#   ./scripts/mctl.sh lambda builtin intraweb   # intraweb だけ同梱実装へ戻す
set -euo pipefail

action="${1:-status}"
arg2="${2:-all}"
arg3="${3:-all}"

variant=""
service="$arg2"
# lambda のときは 2 番目の引数を variant として扱う (サービス名でも all でもない場合)
if [ "$action" = "lambda" ]; then
  case "$arg2" in
    intraweb|interapi|intraapi|sfapi|all) ;;
    *) variant="$arg2"; service="$arg3" ;;
  esac
fi

admin_port() {
  case "$1" in
    intraweb) echo 9081 ;;
    interapi) echo 9082 ;;
    intraapi) echo 9083 ;;
    sfapi)    echo 9084 ;;
    *) echo "unknown service: $1" >&2; exit 1 ;;
  esac
}

targets() {
  if [ "$service" = "all" ]; then echo "intraweb interapi intraapi sfapi"; else echo "$service"; fi
}

for s in $(targets); do
  base="http://localhost:$(admin_port "$s")"
  case "$action" in
    status) curl -s "$base/admin/state" ; echo ;;
    on)     curl -s -X POST "$base/admin/maintenance/on" ; echo ;;
    off)    curl -s -X POST "$base/admin/maintenance/off" ; echo ;;
    rules)  curl -s "$base/admin/rules" ; echo ;;
    lambda)
      if [ -n "$variant" ]; then
        curl -s -X POST "$base/admin/lambda" -H 'Content-Type: application/json' \
          -d "{\"variant\":\"$variant\",\"note\":\"mctl\"}" ; echo
      else
        curl -s "$base/admin/lambda" ; echo
      fi ;;
    *) echo "usage: $0 {status|on|off|rules|lambda} [intraweb|interapi|intraapi|sfapi|all]" >&2; exit 1 ;;
  esac
done
