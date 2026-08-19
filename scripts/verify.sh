#!/usr/bin/env bash
# ALB リスナールール + Lambda メンテナンス応答の自動検証 (bash 版)
#   ./scripts/verify.sh
#   ./scripts/verify.sh --host maint.example.com
#   ./scripts/verify.sh --host intraweb=intraweb.example.com --status 441
set -uo pipefail

PASS=0; FAIL=0
GREEN='\033[32m'; RED='\033[31m'; CYAN='\033[36m'; NC='\033[0m'

SERVICES="intraweb interapi intraapi sfapi"

# メンテナンス中に期待する HTTP ステータスコード。
# 自作 Lambda (custom) が独自コードを返す場合は --status / MAINT_STATUS_EXPECT で変更する。
EXPECT_STATUS="${MAINT_STATUS_EXPECT:-441}"

usage() {
  cat <<'EOS'
使い方: ./scripts/verify.sh [オプション]

  --host <ホスト名>               全 ALB へ送る Host ヘッダ (例: maint.example.com)
  --host <サービス>=<ホスト名>    サービス別の Host ヘッダ (複数回指定可)
                                  サービス: intraweb / interapi / intraapi / sfapi
  --status <コード>               メンテナンス中に期待する HTTP ステータス (既定: 441)
  -h, --help                      このヘルプ

環境変数でも同じ指定ができます:
  VERIFY_HOST=maint.example.com        全サービス共通の Host ヘッダ
  HOST_INTRAWEB=intraweb.example.com   サービス別の Host ヘッダ (HOST_<SERVICE>)
  MAINT_STATUS_EXPECT=441              メンテナンス中に期待するステータスコード
  ALB_URL_<SERVICE> / ADMIN_URL_<SERVICE>
                                       接続先の上書き (./scripts/report.sh doctor が出力)

Host ヘッダを指定しない場合は従来どおり接続先ホスト名 (localhost 等) がそのまま送られます。
自作 Lambda が Host ヘッダで処理を分岐する場合は必ず指定してください。
EOS
}

# --host intraweb=xxx / --host xxx を保存する
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
    *)
      VERIFY_HOST="$arg"
      ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host)   [ $# -ge 2 ] || { echo "エラー: --host にはホスト名が必要です" >&2; exit 2; }
              set_host "$2"; shift 2;;
    --host=*) set_host "${1#--host=}"; shift;;
    --status) [ $# -ge 2 ] || { echo "エラー: --status にはコードが必要です" >&2; exit 2; }
              EXPECT_STATUS="$2"; shift 2;;
    --status=*) EXPECT_STATUS="${1#--status=}"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "エラー: 不明なオプション: $1" >&2; usage >&2; exit 2;;
  esac
done

case "$EXPECT_STATUS" in
  ''|*[!0-9]*) echo "エラー: --status は数値で指定してください: $EXPECT_STATUS" >&2; exit 2;;
esac

alb_port()  { case "$1" in intraweb) echo 8081;; interapi) echo 8082;; intraapi) echo 8083;; sfapi) echo 8084;; esac; }
adm_port()  { case "$1" in intraweb) echo 9081;; interapi) echo 9082;; intraapi) echo 9083;; sfapi) echo 9084;; esac; }

# 接続先。localhost に届かない環境 (コンテナ内 / rootless など) では
# ALB_URL_<SERVICE> / ADMIN_URL_<SERVICE> で上書きできる。
# 使う値は ./scripts/report.sh doctor が export 用の行として出力してくれる。
alb()  { local v="ALB_URL_$(echo "$1" | tr '[:lower:]' '[:upper:]')"; local o="${!v:-}"
         if [ -n "$o" ]; then echo "${o%/}"; else echo "http://localhost:$(alb_port "$1")"; fi; }
adm()  { local v="ADMIN_URL_$(echo "$1" | tr '[:lower:]' '[:upper:]')"; local o="${!v:-}"
         if [ -n "$o" ]; then echo "${o%/}"; else echo "http://localhost:$(adm_port "$1")"; fi; }

# 送信する Host ヘッダ。HOST_<SERVICE> > VERIFY_HOST の順で採用し、
# どちらも無ければ空 (= curl が接続先ホスト名をそのまま入れる)。
host_of() { local v="HOST_$(echo "$1" | tr '[:lower:]' '[:upper:]')"; echo "${!v:-${VERIFY_HOST:-}}"; }

maint() { curl -s -X POST "$(adm "$1")/admin/maintenance/$2" > /dev/null; }
# $1=service $2=variant (builtin|custom)
lambda_variant() {
  curl -s -X POST "$(adm "$1")/admin/lambda" \
    -H 'Content-Type: application/json' -d "{\"variant\":\"$2\",\"note\":\"verify\"}" > /dev/null
}

# $1=name $2=condition(0/1) $3=detail
assert() {
  if [ "$2" = "1" ]; then PASS=$((PASS+1)); printf "  ${GREEN}[PASS]${NC} %s\n" "$1"
  else FAIL=$((FAIL+1)); printf "  ${RED}[FAIL]${NC} %s  %s\n" "$1" "${3:-}"; fi
}

# HTTP 実行: 結果を /tmp に保存し、STATUS / HDR / BODY を設定
req() {
  local url="$1"; shift
  local hdrfile bodyfile
  hdrfile=$(mktemp); bodyfile=$(mktemp)
  STATUS=$(curl -s -o "$bodyfile" -D "$hdrfile" -w '%{http_code}' "$@" "$url")
  HDR=$(cat "$hdrfile"); BODY=$(cat "$bodyfile")
  rm -f "$hdrfile" "$bodyfile"
}
# サービス指定の HTTP 実行: $1=service $2=path 以降は curl への追加引数。
# 設定されていれば Host ヘッダを自動で付ける (Lambda 側の host 分岐を検証するため)。
sreq() {
  local svc="$1" path="$2"; shift 2
  local h; h="$(host_of "$svc")"
  if [ -n "$h" ]; then req "$(alb "$svc")$path" -H "Host: $h" "$@"
  else req "$(alb "$svc")$path" "$@"; fi
}
hdr_val() { echo "$HDR" | tr -d '\r' | grep -i "^$1:" | head -1 | cut -d' ' -f2-; }

printf "\n${CYAN}=== 検証条件 ===${NC}\n"
printf "  メンテ中の期待ステータス : %s\n" "$EXPECT_STATUS"
for s in $SERVICES; do
  h="$(host_of "$s")"
  printf "  %-9s %-28s Host: %s\n" "$s" "$(alb "$s")" "${h:-(指定なし)}"
done

printf "\n${CYAN}=== 0. 事前準備: 全 ALB を通常モードへ ===${NC}\n"
for s in $SERVICES; do maint "$s" off; done

printf "\n${CYAN}=== 1. 通常時: ECS サービスへフォワードされる ===${NC}\n"
for s in $SERVICES; do
  sreq "$s" /v1/orders
  [ "$STATUS" = "200" ] && assert "$s : HTTP 200" 1 || assert "$s : HTTP 200" 0 "actual=$STATUS"
  [ "$(hdr_val X-Backend-Service)" = "$s" ] && assert "$s : ECS コンテナが応答" 1 || assert "$s : ECS コンテナが応答" 0 "actual=$(hdr_val X-Backend-Service)"
done

printf "\n${CYAN}=== 2. メンテ中: web(intraweb) は Lambda が HTML を返す ===${NC}\n"
maint intraweb on
sreq intraweb /dashboard
[ "$STATUS" = "$EXPECT_STATUS" ] && assert "intraweb : HTTP $EXPECT_STATUS" 1 || assert "intraweb : HTTP $EXPECT_STATUS" 0 "actual=$STATUS"
case "$(hdr_val Content-Type)" in text/html*) assert "intraweb : Content-Type = text/html" 1;; *) assert "intraweb : Content-Type = text/html" 0 "actual=$(hdr_val Content-Type)";; esac
case "$(hdr_val X-Alb-Target)" in *lambda*) assert "intraweb : Lambda 経由" 1;; *) assert "intraweb : Lambda 経由" 0 "actual=$(hdr_val X-Alb-Target)";; esac
echo "$BODY" | grep -q 'メンテナンス中' && assert "intraweb : メンテ画面の文言を含む" 1 || assert "intraweb : メンテ画面の文言を含む" 0

sreq intraweb /healthz
[ "$STATUS" = "200" ] && assert "intraweb : /healthz はメンテ中も 200" 1 || assert "intraweb : /healthz はメンテ中も 200" 0 "actual=$STATUS"

sreq intraweb /dashboard -H "X-Forwarded-For: 10.0.100.5"
[ "$STATUS" = "200" ] && assert "intraweb : 許可 IP はバイパスして 200" 1 || assert "intraweb : 許可 IP はバイパスして 200" 0 "actual=$STATUS"

printf "\n${CYAN}=== 3. メンテ中: api は Lambda が HTTP ステータスコードを返す ===${NC}\n"
for s in interapi intraapi sfapi; do maint "$s" on; done
for s in interapi intraapi sfapi; do
  sreq "$s" /v1/orders
  [ "$STATUS" = "$EXPECT_STATUS" ] && assert "$s : HTTP $EXPECT_STATUS" 1 || assert "$s : HTTP $EXPECT_STATUS" 0 "actual=$STATUS"
  case "$(hdr_val Content-Type)" in application/json*) assert "$s : Content-Type = application/json" 1;; *) assert "$s : Content-Type = application/json" 0 "actual=$(hdr_val Content-Type)";; esac
  echo "$BODY" | grep -q 'SERVICE_UNDER_MAINTENANCE' && assert "$s : code=SERVICE_UNDER_MAINTENANCE" 1 || assert "$s : code=SERVICE_UNDER_MAINTENANCE" 0
  echo "$BODY" | grep -q '<html' && assert "$s : HTML を返さない" 0 || assert "$s : HTML を返さない" 1
done

sreq interapi /v1/ping
[ "$STATUS" = "200" ] && assert "interapi : /v1/ping はバイパス" 1 || assert "interapi : /v1/ping はバイパス" 0 "actual=$STATUS"

sreq intraapi /v1/orders -H "X-Maintenance-Bypass: ops-secret-token"
[ "$STATUS" = "200" ] && assert "intraapi : 運用ヘッダでバイパス" 1 || assert "intraapi : 運用ヘッダでバイパス" 0 "actual=$STATUS"

sreq sfapi /internal/sync
[ "$(hdr_val X-Alb-Target)" = "fixed-response" ] && assert "sfapi : /internal/* は ALB fixed-response" 1 || assert "sfapi : /internal/* は ALB fixed-response" 0 "actual=$(hdr_val X-Alb-Target)"

printf "\n${CYAN}=== 4. ALB ごとに独立して切り替わる ===${NC}\n"
maint interapi off
sreq interapi /v1/orders; s1=$STATUS
sreq intraapi /v1/orders; s2=$STATUS
[ "$s1" = "200" ] && assert "interapi のみ復旧 -> 200" 1 || assert "interapi のみ復旧 -> 200" 0 "actual=$s1"
[ "$s2" = "$EXPECT_STATUS" ] && assert "intraapi はメンテ継続 -> $EXPECT_STATUS" 1 || assert "intraapi はメンテ継続 -> $EXPECT_STATUS" 0 "actual=$s2"

printf "\n${CYAN}=== 5. Lambda 実装の差し替え (builtin <-> custom) ===${NC}\n"
maint intraweb on; maint sfapi on
for s in intraweb sfapi; do lambda_variant "$s" builtin; done
sreq intraweb /dashboard
[ "$(hdr_val X-Alb-Lambda-Variant)" = "builtin" ] && assert "intraweb : 既定は builtin" 1 || assert "intraweb : 既定は builtin" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"

for s in intraweb sfapi; do lambda_variant "$s" custom; done
sreq intraweb /dashboard
[ "$STATUS" = "$EXPECT_STATUS" ] && assert "intraweb : 差し替え後も $EXPECT_STATUS" 1 || assert "intraweb : 差し替え後も $EXPECT_STATUS" 0 "actual=$STATUS"
[ "$(hdr_val X-Alb-Lambda-Variant)" = "custom" ] && assert "intraweb : 自作 Lambda が応答" 1 || assert "intraweb : 自作 Lambda が応答" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"
[ "$(hdr_val X-Maintenance-Impl)" = "custom" ] && assert "intraweb : 自作実装の目印ヘッダ" 1 || assert "intraweb : 自作実装の目印ヘッダ" 0 "actual=$(hdr_val X-Maintenance-Impl)"
case "$(hdr_val Content-Type)" in text/html*) assert "intraweb : web なので HTML" 1;; *) assert "intraweb : web なので HTML" 0 "actual=$(hdr_val Content-Type)";; esac

sreq sfapi /v1/orders
[ "$(hdr_val X-Alb-Lambda-Variant)" = "custom" ] && assert "sfapi : 自作 Lambda が応答" 1 || assert "sfapi : 自作 Lambda が応答" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"
case "$(hdr_val Content-Type)" in application/json*) assert "sfapi : api なので JSON" 1;; *) assert "sfapi : api なので JSON" 0 "actual=$(hdr_val Content-Type)";; esac
echo "$BODY" | grep -q '"service": *"sfapi"' && assert "sfapi : 自作 Lambda もサービスを識別" 1 || assert "sfapi : 自作 Lambda もサービスを識別" 0

for s in intraweb sfapi; do lambda_variant "$s" builtin; done
sreq intraweb /dashboard
[ "$(hdr_val X-Alb-Lambda-Variant)" = "builtin" ] && assert "intraweb : builtin に戻る" 1 || assert "intraweb : builtin に戻る" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"

printf "\n${CYAN}=== 6. 後片付け ===${NC}\n"
for s in $SERVICES; do maint "$s" off; done
for s in $SERVICES; do
  sreq "$s" /
  [ "$STATUS" = "200" ] && assert "$s : 復旧後 200" 1 || assert "$s : 復旧後 200" 0 "actual=$STATUS"
done

echo ""
echo "============================================================"
printf "結果: PASS=%s FAIL=%s\n" "$PASS" "$FAIL"
echo "============================================================"
[ "$FAIL" -eq 0 ] || exit 1
