#!/usr/bin/env bash
# ALB リスナールール + Lambda メンテナンス応答の自動検証 (bash 版)
#   ./scripts/verify.sh
set -uo pipefail

PASS=0; FAIL=0
GREEN='\033[32m'; RED='\033[31m'; CYAN='\033[36m'; NC='\033[0m'

alb_port()  { case "$1" in intraweb) echo 8081;; interapi) echo 8082;; intraapi) echo 8083;; sfapi) echo 8084;; esac; }
adm_port()  { case "$1" in intraweb) echo 9081;; interapi) echo 9082;; intraapi) echo 9083;; sfapi) echo 9084;; esac; }

maint() { curl -s -X POST "http://localhost:$(adm_port "$1")/admin/maintenance/$2" > /dev/null; }
# $1=service $2=variant (builtin|custom)
lambda_variant() {
  curl -s -X POST "http://localhost:$(adm_port "$1")/admin/lambda" \
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
hdr_val() { echo "$HDR" | tr -d '\r' | grep -i "^$1:" | head -1 | cut -d' ' -f2-; }

printf "\n${CYAN}=== 0. 事前準備: 全 ALB を通常モードへ ===${NC}\n"
for s in intraweb interapi intraapi sfapi; do maint "$s" off; done

printf "\n${CYAN}=== 1. 通常時: ECS サービスへフォワードされる ===${NC}\n"
for s in intraweb interapi intraapi sfapi; do
  req "http://localhost:$(alb_port $s)/v1/orders"
  [ "$STATUS" = "200" ] && assert "$s : HTTP 200" 1 || assert "$s : HTTP 200" 0 "actual=$STATUS"
  [ "$(hdr_val X-Backend-Service)" = "$s" ] && assert "$s : ECS コンテナが応答" 1 || assert "$s : ECS コンテナが応答" 0 "actual=$(hdr_val X-Backend-Service)"
done

printf "\n${CYAN}=== 2. メンテ中: web(intraweb) は Lambda が HTML を返す ===${NC}\n"
maint intraweb on
req "http://localhost:8081/dashboard"
[ "$STATUS" = "503" ] && assert "intraweb : HTTP 503" 1 || assert "intraweb : HTTP 503" 0 "actual=$STATUS"
case "$(hdr_val Content-Type)" in text/html*) assert "intraweb : Content-Type = text/html" 1;; *) assert "intraweb : Content-Type = text/html" 0 "actual=$(hdr_val Content-Type)";; esac
case "$(hdr_val X-Alb-Target)" in *lambda*) assert "intraweb : Lambda 経由" 1;; *) assert "intraweb : Lambda 経由" 0 "actual=$(hdr_val X-Alb-Target)";; esac
echo "$BODY" | grep -q 'メンテナンス中' && assert "intraweb : メンテ画面の文言を含む" 1 || assert "intraweb : メンテ画面の文言を含む" 0

req "http://localhost:8081/healthz"
[ "$STATUS" = "200" ] && assert "intraweb : /healthz はメンテ中も 200" 1 || assert "intraweb : /healthz はメンテ中も 200" 0 "actual=$STATUS"

req "http://localhost:8081/dashboard" -H "X-Forwarded-For: 10.0.100.5"
[ "$STATUS" = "200" ] && assert "intraweb : 許可 IP はバイパスして 200" 1 || assert "intraweb : 許可 IP はバイパスして 200" 0 "actual=$STATUS"

printf "\n${CYAN}=== 3. メンテ中: api は Lambda が HTTP ステータスコードを返す ===${NC}\n"
for s in interapi intraapi sfapi; do maint "$s" on; done
for s in interapi intraapi sfapi; do
  req "http://localhost:$(alb_port $s)/v1/orders"
  [ "$STATUS" = "503" ] && assert "$s : HTTP 503" 1 || assert "$s : HTTP 503" 0 "actual=$STATUS"
  case "$(hdr_val Content-Type)" in application/json*) assert "$s : Content-Type = application/json" 1;; *) assert "$s : Content-Type = application/json" 0 "actual=$(hdr_val Content-Type)";; esac
  echo "$BODY" | grep -q 'SERVICE_UNDER_MAINTENANCE' && assert "$s : code=SERVICE_UNDER_MAINTENANCE" 1 || assert "$s : code=SERVICE_UNDER_MAINTENANCE" 0
  echo "$BODY" | grep -q '<html' && assert "$s : HTML を返さない" 0 || assert "$s : HTML を返さない" 1
done

req "http://localhost:8082/v1/ping"
[ "$STATUS" = "200" ] && assert "interapi : /v1/ping はバイパス" 1 || assert "interapi : /v1/ping はバイパス" 0 "actual=$STATUS"

req "http://localhost:8083/v1/orders" -H "X-Maintenance-Bypass: ops-secret-token"
[ "$STATUS" = "200" ] && assert "intraapi : 運用ヘッダでバイパス" 1 || assert "intraapi : 運用ヘッダでバイパス" 0 "actual=$STATUS"

req "http://localhost:8084/internal/sync"
[ "$(hdr_val X-Alb-Target)" = "fixed-response" ] && assert "sfapi : /internal/* は ALB fixed-response" 1 || assert "sfapi : /internal/* は ALB fixed-response" 0 "actual=$(hdr_val X-Alb-Target)"

printf "\n${CYAN}=== 4. ALB ごとに独立して切り替わる ===${NC}\n"
maint interapi off
req "http://localhost:8082/v1/orders"; s1=$STATUS
req "http://localhost:8083/v1/orders"; s2=$STATUS
[ "$s1" = "200" ] && assert "interapi のみ復旧 -> 200" 1 || assert "interapi のみ復旧 -> 200" 0 "actual=$s1"
[ "$s2" = "503" ] && assert "intraapi はメンテ継続 -> 503" 1 || assert "intraapi はメンテ継続 -> 503" 0 "actual=$s2"

printf "\n${CYAN}=== 5. Lambda 実装の差し替え (builtin <-> custom) ===${NC}\n"
maint intraweb on; maint sfapi on
for s in intraweb sfapi; do lambda_variant "$s" builtin; done
req "http://localhost:8081/dashboard"
[ "$(hdr_val X-Alb-Lambda-Variant)" = "builtin" ] && assert "intraweb : 既定は builtin" 1 || assert "intraweb : 既定は builtin" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"

for s in intraweb sfapi; do lambda_variant "$s" custom; done
req "http://localhost:8081/dashboard"
[ "$STATUS" = "503" ] && assert "intraweb : 差し替え後も 503" 1 || assert "intraweb : 差し替え後も 503" 0 "actual=$STATUS"
[ "$(hdr_val X-Alb-Lambda-Variant)" = "custom" ] && assert "intraweb : 自作 Lambda が応答" 1 || assert "intraweb : 自作 Lambda が応答" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"
[ "$(hdr_val X-Maintenance-Impl)" = "custom" ] && assert "intraweb : 自作実装の目印ヘッダ" 1 || assert "intraweb : 自作実装の目印ヘッダ" 0 "actual=$(hdr_val X-Maintenance-Impl)"
case "$(hdr_val Content-Type)" in text/html*) assert "intraweb : web なので HTML" 1;; *) assert "intraweb : web なので HTML" 0 "actual=$(hdr_val Content-Type)";; esac

req "http://localhost:8084/v1/orders"
[ "$(hdr_val X-Alb-Lambda-Variant)" = "custom" ] && assert "sfapi : 自作 Lambda が応答" 1 || assert "sfapi : 自作 Lambda が応答" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"
case "$(hdr_val Content-Type)" in application/json*) assert "sfapi : api なので JSON" 1;; *) assert "sfapi : api なので JSON" 0 "actual=$(hdr_val Content-Type)";; esac
echo "$BODY" | grep -q '"service": *"sfapi"' && assert "sfapi : 自作 Lambda もサービスを識別" 1 || assert "sfapi : 自作 Lambda もサービスを識別" 0

for s in intraweb sfapi; do lambda_variant "$s" builtin; done
req "http://localhost:8081/dashboard"
[ "$(hdr_val X-Alb-Lambda-Variant)" = "builtin" ] && assert "intraweb : builtin に戻る" 1 || assert "intraweb : builtin に戻る" 0 "actual=$(hdr_val X-Alb-Lambda-Variant)"

printf "\n${CYAN}=== 6. 後片付け ===${NC}\n"
for s in intraweb interapi intraapi sfapi; do maint "$s" off; done
for s in intraweb interapi intraapi sfapi; do
  req "http://localhost:$(alb_port $s)/"
  [ "$STATUS" = "200" ] && assert "$s : 復旧後 200" 1 || assert "$s : 復旧後 200" 0 "actual=$STATUS"
done

echo ""
echo "============================================================"
printf "結果: PASS=%s FAIL=%s\n" "$PASS" "$FAIL"
echo "============================================================"
[ "$FAIL" -eq 0 ] || exit 1
