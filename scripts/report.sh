#!/usr/bin/env bash
# レスポンス確認ツール albcheck のラッパ (bash / Git Bash / WSL / macOS / Linux)
#
#   ./scripts/report.sh                            # 全シナリオ検証 + Excel レポート出力
#   ./scripts/report.sh report --variant both      # builtin と custom を続けて検証
#   ./scripts/report.sh report --contract          # Lambda 契約チェックも実施
#   ./scripts/report.sh check intraweb /dashboard  # 1 リクエストの詳細 + 画面表示
#   ./scripts/report.sh contract --variant custom  # 自作 Lambda の契約チェック
#   ./scripts/report.sh variant custom             # invoke 先 Lambda を差し替える
#   ./scripts/report.sh doctor                     # 繋がらないときの原因診断
#
# 実行場所の選び方 (先頭に付けるオプション):
#   --host           ホストの Python で実行する (自動フォールバックしない)
#   --in-container   検証用コンテナの中から実行する (ホストのポートに届かない環境向け)
#   (無指定)         ホストから届くかを確認し、届かなければコンテナ内実行へ自動で切り替える
#
# ALB へ送る Host ヘッダ / メンテ中に期待するステータスコード:
#   ./scripts/report.sh report --host maint.example.com --status 441
#   ./scripts/report.sh --alb-host maint.example.com report      # 先頭に置く場合
#   ※ 先頭の --host は上記のとおり「ホストで実行する」の意味なので、
#     サブコマンドより前に置くときは --alb-host を使ってください。
#   環境変数 VERIFY_HOST / HOST_<SERVICE> / MAINT_STATUS_EXPECT でも指定できます
#   (コンテナ内実行のときも引き継ぎます)。
#
# ブラウザは一切不要です。メンテナンス画面は check / render がテキスト描画します。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool="$root/tools/albcheck.py"

mode=auto
globals=()   # albcheck へ渡すグローバルオプション (サブコマンドより前に置く必要がある)
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host)                    mode=host;      shift ;;
    --in-container|--container) mode=container; shift ;;
    # ALB へ送る Host ヘッダ。先頭の --host は「ホストで実行」の意味なので別名にする
    --alb-host)   [ "$#" -ge 2 ] || { echo "エラー: --alb-host にはホスト名が必要です" >&2; exit 2; }
                  globals+=(--host "$2"); shift 2 ;;
    --alb-host=*) globals+=(--host "${1#--alb-host=}"); shift ;;
    --status)     [ "$#" -ge 2 ] || { echo "エラー: --status にはコードが必要です" >&2; exit 2; }
                  globals+=(--status "$2"); shift 2 ;;
    --status=*)   globals+=(--status "${1#--status=}"); shift ;;
    *) break ;;
  esac
done

if [ "$#" -eq 0 ]; then set -- report; fi

export PYTHONIOENCODING=utf-8

# 実際に動く Python を選ぶ (Windows Store のスタブなど、名前だけあるものを避ける)
python_bin=""
for bin in python3 python; do
  command -v "$bin" >/dev/null 2>&1 || continue
  "$bin" -c "import sys" >/dev/null 2>&1 || continue
  python_bin="$bin"
  break
done

# --- コンテナランタイムと compose コマンドを探す --------------------------------
# RHEL / Amazon Linux では docker ではなく podman が入っていることが多い。
runtime=""
compose=""
for rt in docker podman nerdctl; do
  command -v "$rt" >/dev/null 2>&1 || continue
  "$rt" info >/dev/null 2>&1 || continue
  runtime="$rt"
  if "$rt" compose version >/dev/null 2>&1; then
    compose="$rt compose"
  elif command -v "${rt}-compose" >/dev/null 2>&1; then
    compose="${rt}-compose"
  elif [ "$rt" = "docker" ] && command -v docker-compose >/dev/null 2>&1; then
    compose="docker-compose"
  fi
  break
done

run_on_host() {
  exec "$python_bin" "$tool" ${globals[@]+"${globals[@]}"} "$@"
}

# Host ヘッダ / 期待ステータスの指定はコンテナの中へも引き継ぐ
# (verify.sh / mctl.sh と同じ環境変数名)
env_args() {
  local name
  for name in VERIFY_HOST HOST_INTRAWEB HOST_INTERAPI HOST_INTRAAPI HOST_SFAPI \
              MAINT_STATUS_EXPECT; do
    if [ -n "${!name:-}" ]; then printf '%s\n' "-e" "$name=${!name}"; fi
  done
}

# コンテナの中から albcheck を動かす。
#   1) compose があれば inspector サービス (w3m/lynx 入り) で実行
#   2) 無ければ ALB コンテナと同じネットワークに使い捨てコンテナを立てて実行
run_in_container() {
  if [ -z "$runtime" ]; then
    echo "エラー: 使えるコンテナランタイム (docker / podman / nerdctl) がありません。" >&2
    echo "        RHEL 9: sudo dnf install -y podman podman-compose" >&2
    exit 3
  fi

  local envs=()
  while IFS= read -r line; do envs+=("$line"); done < <(env_args)

  if [ -n "$compose" ]; then
    echo "[コンテナ内で実行] $compose run --rm inspector $*" >&2
    cd "$root"
    exec $compose run --rm ${envs[@]+"${envs[@]}"} inspector \
      ${globals[@]+"${globals[@]}"} "$@"
  fi

  local network
  network="$("$runtime" inspect -f \
    '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' alb-intraweb 2>/dev/null || true)"
  if [ -z "$network" ]; then
    echo "エラー: compose コマンドが無く、alb-intraweb のネットワークも特定できません。" >&2
    echo "        検証環境が起動しているか確認してください: $runtime ps -a" >&2
    exit 3
  fi

  # SELinux が Enforcing のホストではバインドマウントに :z (再ラベル) が要る
  local ro_opt=":ro"
  local rw_opt=""
  if [ -f /sys/fs/selinux/enforce ] && [ "$(cat /sys/fs/selinux/enforce)" = "1" ]; then
    ro_opt=":ro,z"
    rw_opt=":z"
  fi

  echo "[コンテナ内で実行] $runtime run --rm --network $network ... $*" >&2
  exec "$runtime" run --rm --network "$network" \
    -v "$root/tools:/work/tools$ro_opt" \
    -v "$root/reports:/work/reports$rw_opt" \
    -e PYTHONIOENCODING=utf-8 \
    -e ALB_URL_INTRAWEB=http://alb-intraweb \
    -e ALB_URL_INTERAPI=http://alb-interapi \
    -e ALB_URL_INTRAAPI=http://alb-intraapi \
    -e ALB_URL_SFAPI=http://alb-sfapi \
    -e ADMIN_URL_INTRAWEB=http://alb-intraweb:9000 \
    -e ADMIN_URL_INTERAPI=http://alb-interapi:9000 \
    -e ADMIN_URL_INTRAAPI=http://alb-intraapi:9000 \
    -e ADMIN_URL_SFAPI=http://alb-sfapi:9000 \
    -e LAMBDA_URL_BUILTIN=http://maintenance-lambda:8080/2015-03-31/functions/function/invocations \
    -e LAMBDA_URL_CUSTOM=http://custom-lambda:8080/2015-03-31/functions/function/invocations \
    -e REPORT_DIR=/work/reports \
    ${envs[@]+"${envs[@]}"} \
    python:3.12-slim python /work/tools/albcheck.py \
    ${globals[@]+"${globals[@]}"} "$@"
}

case "$mode" in
  container)
    run_in_container "$@"
    ;;
  host)
    if [ -z "$python_bin" ]; then
      echo "エラー: --host を指定しましたが Python が見つかりません。" >&2
      exit 3
    fi
    run_on_host "$@"
    ;;
  *)
    if [ -z "$python_bin" ]; then
      echo "ホストに Python が無いためコンテナ内で実行します" >&2
      run_in_container "$@"
    fi
    # doctor 自体は「繋がらない理由」を出すのが仕事なので、必ずホストで実行する
    # (--no-color などが前に付いていてもよいよう、引数全体から探す)
    for arg in "$@"; do
      if [ "$arg" = "doctor" ]; then
        run_on_host "$@"
      fi
    done
    # ホストから検証環境へ届くか先に確認する (届けばそのままホストで実行)
    if "$python_bin" "$tool" doctor --quiet >/dev/null 2>&1; then
      run_on_host "$@"
    fi
    if [ -n "$runtime" ]; then
      echo "ホストから検証環境へ届かないため、コンテナの中から実行します" >&2
      echo "  (ホストで実行したい場合は --host、原因を見るには ./scripts/report.sh doctor)" >&2
      run_in_container "$@"
    fi
    # コンテナ経由も無理なら、ホストで実行して albcheck に診断を出させる
    run_on_host "$@"
    ;;
esac
