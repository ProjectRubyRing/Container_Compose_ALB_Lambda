#!/usr/bin/env bash
# メンテナンスモード / Lambda 差し替え操作ツール (bash / Git Bash / WSL / macOS / Linux)
#   ./scripts/mctl.sh status
#   ./scripts/mctl.sh on  intraweb
#   ./scripts/mctl.sh off all
#   ./scripts/mctl.sh rules sfapi
#   ./scripts/mctl.sh lambda                    # 現在の Lambda 実装 (variant) を表示
#   ./scripts/mctl.sh lambda custom             # 全 ALB を自作 Lambda へ差し替え
#   ./scripts/mctl.sh lambda builtin intraweb   # intraweb だけ同梱実装へ戻す
#
# 管理 API へ送る Host ヘッダと、期待する HTTP ステータスも指定できる:
#   ./scripts/mctl.sh --host maint.example.com on intraweb
#   ./scripts/mctl.sh --host intraweb=web.example.com --status 200 status
#   ※ 応答本文 (JSON) は従来どおり標準出力へ、ステータス行は標準エラーへ出します。
set -euo pipefail

usage() {
  cat <<'EOS'
使い方: ./scripts/mctl.sh [オプション] {status|on|off|rules|lambda} [サービス|all]

オプション:
  --host <ホスト名>              管理 API へ送る Host ヘッダ
  --host <サービス>=<ホスト名>   サービス別の Host ヘッダ (複数回指定可)
                                 サービス: intraweb / interapi / intraapi / sfapi
  --status <コード>              期待する HTTP ステータス (既定 200 / 不一致なら異常終了)
  -h, --help                     このヘルプ

環境変数でも指定できます (verify.sh / report.sh と共通):
  VERIFY_HOST / HOST_<SERVICE>   Host ヘッダ
  ADMIN_STATUS_EXPECT            期待する HTTP ステータス
  ADMIN_URL_<SERVICE>            管理 API の接続先 (./scripts/report.sh doctor が出力)
EOS
}

SERVICES="intraweb interapi intraapi sfapi"
expect_status="${ADMIN_STATUS_EXPECT:-200}"

set_host() {
  local arg="$1" svc name upper
  case "$arg" in
    *=*)
      svc="${arg%%=*}"; name="${arg#*=}"
      case " $SERVICES " in
        *" $svc "*) ;;
        *) echo "エラー: 未知のサービス名です: $svc ($SERVICES のいずれか)" >&2; exit 2;;
      esac
      upper=$(echo "$svc" | tr '[:lower:]' '[:upper:]')
      eval "HOST_$upper=\$name"
      ;;
    *) VERIFY_HOST="$arg" ;;
  esac
}

# オプションと位置引数を分ける (オプションはどこに書いてもよい)
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host)     [ "$#" -ge 2 ] || { echo "エラー: --host にはホスト名が必要です" >&2; exit 2; }
                set_host "$2"; shift 2 ;;
    --host=*)   set_host "${1#--host=}"; shift ;;
    --status)   [ "$#" -ge 2 ] || { echo "エラー: --status にはコードが必要です" >&2; exit 2; }
                expect_status="$2"; shift 2 ;;
    --status=*) expect_status="${1#--status=}"; shift ;;
    -h|--help)  usage; exit 0 ;;
    --) shift; while [ "$#" -gt 0 ]; do args+=("$1"); shift; done ;;
    -*) echo "エラー: 不明なオプション: $1" >&2; usage >&2; exit 2 ;;
    *)  args+=("$1"); shift ;;
  esac
done
set -- ${args[@]+"${args[@]}"}

case "$expect_status" in
  ''|*[!0-9]*) echo "エラー: --status は数値で指定してください: $expect_status" >&2; exit 2;;
esac

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

# 管理 API の URL。localhost に届かない環境 (コンテナ内 / rootless など) では
# ADMIN_URL_<SERVICE> で上書きできる。値は ./scripts/report.sh doctor が教えてくれる。
admin_base() {
  local var="ADMIN_URL_$(echo "$1" | tr '[:lower:]' '[:upper:]')"
  local override="${!var:-}"
  if [ -n "$override" ]; then echo "${override%/}"; else echo "http://localhost:$(admin_port "$1")"; fi
}

# 送信する Host ヘッダ。HOST_<SERVICE> > VERIFY_HOST の順で採用し、
# どちらも無ければ空 (= curl が接続先ホスト名をそのまま入れる)。
host_of() {
  local var="HOST_$(echo "$1" | tr '[:lower:]' '[:upper:]')"
  echo "${!var:-${VERIFY_HOST:-}}"
}

targets() {
  if [ "$service" = "all" ]; then echo "$SERVICES"; else echo "$service"; fi
}

rc=0
# $1=service $2=method $3=path [$4=JSON body]
# 本文を標準出力へ、"サービス HTTP コード" を標準エラーへ出し、期待と違えば rc=1
call() {
  local svc="$1" method="$2" path="$3" data="${4:-}"
  local host bodyfile code curl_args
  host="$(host_of "$svc")"
  bodyfile=$(mktemp)
  curl_args=(-s -o "$bodyfile" -w '%{http_code}' -X "$method")
  [ -n "$host" ] && curl_args+=(-H "Host: $host")
  [ -n "$data" ] && curl_args+=(-H 'Content-Type: application/json' -d "$data")
  code=$(curl "${curl_args[@]}" "$(admin_base "$svc")$path" || true)
  cat "$bodyfile"; echo
  rm -f "$bodyfile"

  if [ -z "$code" ] || [ "$code" = "000" ]; then
    echo "  $svc : 管理 API へ接続できません ($(admin_base "$svc")$path)" >&2
    rc=1
    return
  fi
  echo "  $svc : HTTP $code${host:+  (Host: $host)}" >&2
  if [ "$code" != "$expect_status" ]; then
    echo "  $svc : 期待したステータス $expect_status ではありません (実際 $code)" >&2
    rc=1
  fi
}

for s in $(targets); do
  case "$action" in
    status) call "$s" GET  /admin/state ;;
    on)     call "$s" POST /admin/maintenance/on ;;
    off)    call "$s" POST /admin/maintenance/off ;;
    rules)  call "$s" GET  /admin/rules ;;
    lambda)
      if [ -n "$variant" ]; then
        call "$s" POST /admin/lambda "{\"variant\":\"$variant\",\"note\":\"mctl\"}"
      else
        call "$s" GET /admin/lambda
      fi ;;
    *) usage >&2; exit 1 ;;
  esac
done
exit "$rc"
