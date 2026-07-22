#!/usr/bin/env bash
# メンテナンスモード操作ツール (bash / Git Bash / WSL / macOS / Linux)
#   ./scripts/mctl.sh status
#   ./scripts/mctl.sh on  intraweb
#   ./scripts/mctl.sh off all
#   ./scripts/mctl.sh rules sfapi
set -euo pipefail

action="${1:-status}"
service="${2:-all}"

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
    *) echo "usage: $0 {status|on|off|rules} [intraweb|interapi|intraapi|sfapi|all]" >&2; exit 1 ;;
  esac
done
