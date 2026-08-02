#!/usr/bin/env bash
# レスポンス確認ツール albcheck のラッパ (bash / Git Bash / WSL / macOS / Linux)
#
#   ./scripts/report.sh                            # 全シナリオ検証 + Excel レポート出力
#   ./scripts/report.sh report --variant both      # builtin と custom を続けて検証
#   ./scripts/report.sh report --contract          # Lambda 契約チェックも実施
#   ./scripts/report.sh check intraweb /dashboard  # 1 リクエストの詳細 + 画面表示
#   ./scripts/report.sh contract --variant custom  # 自作 Lambda の契約チェック
#   ./scripts/report.sh variant custom             # invoke 先 Lambda を差し替える
#
# ホストに python3 があればそれで、無ければ docker compose run --rm inspector で実行する。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool="$root/tools/albcheck.py"

if [ "$#" -eq 0 ]; then set -- report; fi

export PYTHONIOENCODING=utf-8
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$tool" "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$tool" "$@"
fi

echo "ホストに Python が無いため docker compose run --rm inspector で実行します" >&2
cd "$root"
exec docker compose run --rm inspector "$@"
