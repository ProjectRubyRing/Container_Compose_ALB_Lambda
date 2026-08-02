#!/usr/bin/env python3
"""
albcheck — ALB × Lambda メンテナンス応答の呼び出し確認ツール (標準ライブラリのみ)

    python tools/albcheck.py check    intraweb /dashboard   # 1 リクエストの詳細 + 画面表示
    python tools/albcheck.py report   --excel reports/r.xlsx # 全シナリオ検証 + Excel レポート
    python tools/albcheck.py contract --variant custom       # 自作 Lambda が契約を守っているか
    python tools/albcheck.py variant  custom                 # invoke 先 Lambda を差し替える
    python tools/albcheck.py render   http://localhost:8081/ # 画面をテキストブラウザ表示

pip install は不要 (urllib + 自作 xlsx ライタ)。接続先はすべて環境変数で上書きできる:
    ALB_URL_<SERVICE>     例 ALB_URL_INTRAWEB=http://alb-intraweb
    ADMIN_URL_<SERVICE>   例 ADMIN_URL_INTRAWEB=http://alb-intraweb:9000
    LAMBDA_URL_<VARIANT>  例 LAMBDA_URL_CUSTOM=http://custom-lambda:8080/...
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import textrender as T  # noqa: E402

# ---------------------------------------------------------------------------
# 接続先
# ---------------------------------------------------------------------------
SERVICES = ["intraweb", "interapi", "intraapi", "sfapi"]
WEB_SERVICES = ["intraweb"]
_ALB_PORTS = {"intraweb": 8081, "interapi": 8082, "intraapi": 8083, "sfapi": 8084}
_ADMIN_PORTS = {"intraweb": 9081, "interapi": 9082, "intraapi": 9083, "sfapi": 9084}
_LAMBDA_PORTS = {"builtin": 9001, "custom": 9002}
RIE_PATH = "/2015-03-31/functions/function/invocations"

# Lambda が web/api を判定するのに使うターゲットグループ ARN (config/*.yaml と同じもの)
TARGET_GROUP_ARNS = {
    "intraweb": "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:"
                "targetgroup/tg-intraweb-maintenance/1111111111111111",
    "interapi": "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:"
                "targetgroup/tg-interapi-maintenance/2222222222222222",
    "intraapi": "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:"
                "targetgroup/tg-intraapi-maintenance/3333333333333333",
    "sfapi": "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:"
             "targetgroup/tg-sfapi-maintenance/4444444444444444",
}


def alb_url(service):
    return os.environ.get("ALB_URL_" + service.upper(),
                          "http://localhost:%d" % _ALB_PORTS[service]).rstrip("/")


def admin_url(service):
    return os.environ.get("ADMIN_URL_" + service.upper(),
                          "http://localhost:%d" % _ADMIN_PORTS[service]).rstrip("/")


def lambda_url(variant):
    default = "http://localhost:%d%s" % (_LAMBDA_PORTS.get(variant, 9001), RIE_PATH)
    return os.environ.get("LAMBDA_URL_" + variant.upper(), default)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """ALB の redirect アクションを検証したいのでリダイレクトは追わない"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Response:
    def __init__(self, url, method, status=0, reason="", headers=None, body="",
                 size=0, elapsed_ms=0.0, error=None, req_headers=None):
        self.url = url
        self.method = method
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.body = body
        self.size = size
        self.elapsed_ms = elapsed_ms
        self.error = error
        self.req_headers = req_headers or {}

    def header(self, name, default=""):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default

    @property
    def content_type(self):
        return self.header("Content-Type")

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, TypeError):
            return None


def http(url, method="GET", headers=None, data=None, timeout=20):
    headers = dict(headers or {})
    if isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    started = time.perf_counter()
    try:
        resp = _OPENER.open(req, timeout=timeout)
        status, reason, raw = resp.status, resp.reason, resp.read()
        hdrs = dict(resp.headers.items())
        resp.close()
    except urllib.error.HTTPError as e:      # 4xx/5xx も本文まで読む
        status, reason, raw = e.code, e.reason, e.read()
        hdrs = dict(e.headers.items())
        e.close()
    except Exception as e:                   # 接続不可・タイムアウトなど
        return Response(url, method, error=str(e), req_headers=headers,
                        elapsed_ms=(time.perf_counter() - started) * 1000)
    elapsed = (time.perf_counter() - started) * 1000
    return Response(url, method, status, str(reason), hdrs,
                    raw.decode("utf-8", "replace"), len(raw), elapsed,
                    req_headers=headers)


# ---------------------------------------------------------------------------
# 管理 API 操作
# ---------------------------------------------------------------------------
def set_maintenance(service, enabled):
    verb = "on" if enabled else "off"
    return http("%s/admin/maintenance/%s" % (admin_url(service), verb), "POST")


def get_state(service):
    return http("%s/admin/state" % admin_url(service)).json() or {}


def set_variant(service, variant):
    return http("%s/admin/lambda" % admin_url(service), "POST",
                {"Content-Type": "application/json"},
                json.dumps({"variant": variant, "note": "albcheck"}))


def get_lambda_info(service):
    return http("%s/admin/lambda" % admin_url(service)).json() or {}


# ---------------------------------------------------------------------------
# 検証項目
# ---------------------------------------------------------------------------
class Check:
    def __init__(self, label, expected, actual, ok, level="必須"):
        self.label = label
        self.expected = expected
        self.actual = actual
        self.ok = ok
        self.level = level

    @property
    def verdict(self):
        if self.ok:
            return "PASS"
        return "FAIL" if self.level == "必須" else "WARN"


def _dig(data, dotted):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def evaluate(resp, expects, variant=None):
    """expects の指定に沿ってレスポンスを検証し Check のリストを返す"""
    checks = []

    def add(label, expected, actual, ok, level="必須"):
        checks.append(Check(label, expected, actual, ok, level))

    if resp.error:
        add("接続", "接続できること", "エラー: %s" % resp.error, False)
        return checks

    if "status" in expects:
        add("HTTP ステータス", expects["status"], resp.status, resp.status == expects["status"])
    if "content_type" in expects:
        want = expects["content_type"]
        add("Content-Type", "%s* " % want, resp.content_type or "(なし)",
            resp.content_type.lower().startswith(want.lower()))
    if "rule" in expects:
        actual = resp.header("X-Alb-Matched-Rule") or "(なし)"
        add("適用リスナールール", expects["rule"], actual, actual == expects["rule"])
    if "target_contains" in expects:
        actual = resp.header("X-Alb-Target") or "(なし)"
        add("転送先ターゲット", "*%s*" % expects["target_contains"], actual,
            expects["target_contains"] in actual)
    if "backend" in expects:
        actual = resp.header("X-Backend-Service") or "(なし)"
        add("応答した ECS", expects["backend"], actual, actual == expects["backend"])
    if "body_contains" in expects:
        add("本文に含まれる文字列", expects["body_contains"],
            "あり" if expects["body_contains"] in resp.body else "なし",
            expects["body_contains"] in resp.body)
    if "body_not_contains" in expects:
        add("本文に含まれない文字列", expects["body_not_contains"],
            "あり" if expects["body_not_contains"] in resp.body else "なし",
            expects["body_not_contains"] not in resp.body)
    for name, want in (expects.get("headers") or {}).items():
        actual = resp.header(name) or "(なし)"
        ok = actual != "(なし)" if want is True else actual == want
        add("ヘッダ %s" % name, "存在すること" if want is True else want, actual, ok)
    for path, want in (expects.get("json") or {}).items():
        actual = _dig(resp.json() or {}, path)
        add("JSON %s" % path, want, actual if actual is not None else "(なし)", actual == want)

    # Lambda 応答なら、意図した実装 (variant) が呼ばれたことも確認する
    if variant and "lambda" in (resp.header("X-Alb-Target") or ""):
        actual = resp.header("X-Alb-Lambda-Variant") or "(なし)"
        add("呼ばれた Lambda 実装", variant, actual, actual == variant)
    return checks


# ---------------------------------------------------------------------------
# ケース / シナリオ定義
# ---------------------------------------------------------------------------
class Case:
    def __init__(self, cid, title, service, path="/", method="GET",
                 headers=None, expects=None, note=""):
        self.id = cid
        self.title = title
        self.service = service
        self.path = path
        self.method = method
        self.headers = headers or {}
        self.expects = expects or {}
        self.note = note


def _normal_cases():
    return [
        Case("N-%d" % (i + 1), "%s: 通常時は ECS へフォワード" % s, s, "/v1/orders",
             expects={"status": 200, "backend": s, "rule": "default",
                      "target_contains": "ecs"},
             note="default action が ECS ターゲットグループを向いている")
        for i, s in enumerate(SERVICES)
    ]


def _api_maint_cases():
    cases = []
    for i, s in enumerate(["interapi", "intraapi", "sfapi"]):
        cases.append(Case(
            "A-%d" % (i + 1), "%s: メンテ中は Lambda が 503 + JSON" % s, s, "/v1/orders",
            expects={"status": 503, "content_type": "application/json",
                     "target_contains": "lambda", "body_not_contains": "<html",
                     "headers": {"X-Maintenance": "true", "Retry-After": True},
                     "json": {"error.code": "SERVICE_UNDER_MAINTENANCE", "error.service": s}},
            note="機械向けなので HTML ではなくステータスコード + JSON を返す"))
    cases += [
        Case("A-4", "interapi: /v1/ping はメンテ中もバイパス (priority 10)", "interapi",
             "/v1/ping",
             expects={"status": 200, "rule": "maintenance-bypass-ping",
                      "target_contains": "ecs"},
             note="path-pattern 条件による疎通確認用バイパス"),
        Case("A-5", "intraapi: 正しい運用ヘッダはバイパス", "intraapi", "/v1/orders",
             headers={"X-Maintenance-Bypass": "ops-secret-token"},
             expects={"status": 200, "rule": "maintenance-bypass-header",
                      "target_contains": "ecs"},
             note="http-header 条件によるバイパス"),
        Case("A-6", "intraapi: 誤った運用ヘッダは 503", "intraapi", "/v1/orders",
             headers={"X-Maintenance-Bypass": "wrong-token"},
             expects={"status": 503, "rule": "maintenance-catch-all",
                      "target_contains": "lambda"},
             note="値が一致しなければバイパスされない"),
        Case("A-7", "sfapi: /internal/* は ALB の fixed-response (Lambda 非経由)", "sfapi",
             "/internal/sync",
             expects={"status": 503, "target_contains": "fixed-response"},
             note="Lambda を使わない比較用ルール"),
    ]
    return cases


PLAN = [
    {
        "group": "1. 通常時",
        "desc": "4 ALB すべてがデフォルトアクションで ECS サービスへフォワードする",
        "maintenance": {s: False for s in SERVICES},
        "cases": _normal_cases(),
    },
    {
        "group": "2. メンテ中 (Web)",
        "desc": "intraweb は Lambda がメンテナンス画面 (HTML) を返す",
        "maintenance": {"intraweb": True},
        "cases": [
            Case("W-1", "intraweb: メンテナンス画面 (HTML) が返る", "intraweb", "/dashboard",
                 expects={"status": 503, "content_type": "text/html",
                          "target_contains": "lambda", "rule": "maintenance-catch-all",
                          "body_contains": "メンテナンス",
                          "headers": {"Retry-After": True, "X-Maintenance": "true",
                                      "X-Maintenance-Backend-Kind": "web"}},
                 note="人が見る画面なので HTML を返す"),
            Case("W-2", "intraweb: /healthz はメンテ中も ECS へ (priority 1)", "intraweb",
                 "/healthz",
                 expects={"status": 200, "rule": "healthcheck-passthrough",
                          "target_contains": "ecs"},
                 note="ヘルスチェックが落ちないようにする"),
            Case("W-3", "intraweb: 運用者セグメントはバイパス (10.0.100.0/24)", "intraweb",
                 "/dashboard", headers={"X-Forwarded-For": "10.0.100.5"},
                 expects={"status": 200, "rule": "maintenance-bypass-source-ip",
                          "target_contains": "ecs"},
                 note="source-ip 条件による動作確認用バイパス"),
            Case("W-4", "intraweb: 対象外 IP はメンテ画面", "intraweb", "/dashboard",
                 headers={"X-Forwarded-For": "192.168.10.5"},
                 expects={"status": 503, "rule": "maintenance-catch-all",
                          "target_contains": "lambda"},
                 note="CIDR 外はバイパスされない"),
        ],
    },
    {
        "group": "3. メンテ中 (API)",
        "desc": "API 3 本は Lambda が HTTP ステータスコード + JSON を返す",
        "maintenance": {"interapi": True, "intraapi": True, "sfapi": True},
        "cases": _api_maint_cases(),
    },
    {
        "group": "4. ALB ごとの独立性",
        "desc": "interapi だけ復旧しても他はメンテナンスのまま",
        "maintenance": {"interapi": False},
        "cases": [
            Case("I-1", "interapi: 復旧して 200", "interapi", "/v1/orders",
                 expects={"status": 200, "target_contains": "ecs"}),
            Case("I-2", "intraapi: メンテナンス継続で 503", "intraapi", "/v1/orders",
                 expects={"status": 503, "target_contains": "lambda"}),
        ],
    },
    {
        "group": "5. 復旧",
        "desc": "全 ALB を通常モードへ戻す",
        "maintenance": {s: False for s in SERVICES},
        "cases": [
            Case("R-%d" % (i + 1), "%s: 復旧後 200" % s, s, "/",
                 expects={"status": 200, "target_contains": "ecs"})
            for i, s in enumerate(SERVICES)
        ],
    },
]


# ---------------------------------------------------------------------------
# 実行結果
# ---------------------------------------------------------------------------
class CaseResult:
    def __init__(self, case, group, resp, checks, screen):
        self.case = case
        self.group = group
        self.resp = resp
        self.checks = checks
        self.screen_title, self.screen_lines, self.screen_kind = screen

    @property
    def ok(self):
        return all(c.ok or c.level != "必須" for c in self.checks)

    @property
    def verdict(self):
        if not self.checks:
            return "INFO"
        if any(not c.ok and c.level == "必須" for c in self.checks):
            return "FAIL"
        if any(not c.ok for c in self.checks):
            return "WARN"
        return "PASS"


class Run:
    def __init__(self, variant, width, renderer):
        self.variant = variant
        self.width = width
        self.renderer = renderer
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = ""
        self.results = []
        self.contract = []
        self.elapsed = 0.0

    @property
    def passed(self):
        return sum(1 for r in self.results if r.verdict == "PASS")

    @property
    def failed(self):
        return sum(1 for r in self.results if r.verdict == "FAIL")

    @property
    def warned(self):
        return sum(1 for r in self.results if r.verdict == "WARN")

    @property
    def check_total(self):
        return sum(len(r.checks) for r in self.results)

    @property
    def check_failed(self):
        return sum(1 for r in self.results for c in r.checks
                   if not c.ok and c.level == "必須")


def do_case(case, group, width, renderer, variant=None):
    url = alb_url(case.service) + case.path
    resp = http(url, case.method, case.headers)
    checks = evaluate(resp, case.expects, variant)
    # 枠 (frame) の内側に収まる幅で描画しておく
    screen = T.render_body(resp.content_type, resp.body, max(20, width - 4), renderer)
    return CaseResult(case, group, resp, checks, screen)


# ---------------------------------------------------------------------------
# 画面表示 (レスポンス詳細)
# ---------------------------------------------------------------------------
COLOR = True


def c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if COLOR else str(text)


def _verdict_color(verdict):
    return {"PASS": "1;32", "FAIL": "1;31", "WARN": "1;33"}.get(verdict, "0")


def section(title, width=78):
    bar = "─" * max(0, width - T.display_width(title) - 3)
    print(c("── %s %s" % (title, bar), "38;5;244"))


def print_detail(result, width=78, show_screen=True, show_body=False):
    """1 リクエストのレスポンス詳細を表示する"""
    resp, case = result.resp, result.case
    print()
    print(c("═" * width, "38;5;39"))
    print(" " + c(case.title or "%s %s" % (case.method, case.path), "1"))
    print(c("═" * width, "38;5;39"))

    section("リクエスト", width)
    print("  %s %s" % (c(case.method, "1;36"), resp.url))
    for k, v in (case.headers or {}).items():
        print("    %s: %s" % (c(k, "36"), v))

    if resp.error:
        print()
        print("  " + c("接続エラー: %s" % resp.error, "1;31"))
        return

    section("レスポンス", width)
    status_color = "1;32" if resp.status < 400 else "1;31" if resp.status >= 500 else "1;33"
    print("  %s %s      %s ms / %s bytes" % (
        c(resp.status, status_color), resp.reason,
        c("%.1f" % resp.elapsed_ms, "1"), format(resp.size, ",")))

    route = _route_summary(resp)
    if route:
        print("  経路: " + route)

    groups = [
        ("ALB が付与したヘッダ", lambda k: k.lower().startswith(("x-alb-", "x-amzn-"))),
        ("Lambda / メンテナンス応答のヘッダ", lambda k: k.lower().startswith("x-maintenance")),
        ("バックエンド (ECS) のヘッダ", lambda k: k.lower().startswith("x-backend")),
        ("標準ヘッダ", lambda k: not k.lower().startswith(("x-alb-", "x-amzn-",
                                                          "x-maintenance", "x-backend"))),
    ]
    for label, match in groups:
        items = [(k, v) for k, v in resp.headers.items() if match(k)]
        if not items:
            continue
        section(label, width)
        keylen = max(T.display_width(k) for k, _ in items)
        for k, v in items:
            print("  %s  %s" % (c(T.pad_to_width(k, keylen), "36"),
                                T.trim_to_width(v, width - keylen - 4)))

    if result.checks:
        section("判定", width)
        for chk in result.checks:
            mark = {"PASS": "✔", "FAIL": "✘", "WARN": "▲"}[chk.verdict]
            line = "  %s %s" % (c(mark + " " + chk.verdict, _verdict_color(chk.verdict)),
                                chk.label)
            if not chk.ok:
                line += "   期待=%s / 実際=%s" % (chk.expected, chk.actual)
            print(line)

    if show_screen:
        section("画面表示 (テキストブラウザ描画)  種別=%s" % result.screen_kind, width)
        subtitle = "%s %s  →  %s" % (case.method, resp.url, resp.status)
        framed = T.frame(result.screen_lines, width,
                         title=result.screen_title or resp.content_type.split(";")[0],
                         subtitle=T.trim_to_width(subtitle, width - 4))
        print(T.to_ansi(framed, COLOR))

    if show_body:
        section("レスポンス本文 (生データ)", width)
        print(resp.body)
    print()


def _route_summary(resp):
    parts = []
    alb = resp.header("X-Alb-Name")
    if not alb:
        return ""
    parts.append(c(alb, "1"))
    parts.append("maintenance=%s" % c(resp.header("X-Alb-Maintenance", "?"),
                                      "33" if resp.header("X-Alb-Maintenance") == "true" else "32"))
    rule = resp.header("X-Alb-Matched-Rule")
    if rule:
        parts.append("rule=%s(prio %s)" % (c(rule, "1;36"), resp.header("X-Alb-Rule-Priority", "-")))
    target = resp.header("X-Alb-Target")
    if target:
        variant = resp.header("X-Alb-Lambda-Variant")
        parts.append("→ %s%s" % (c(target, "1;35"),
                                 c("[%s]" % variant, "1;33") if variant else ""))
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Lambda 契約チェック
# ---------------------------------------------------------------------------
def elb_event(service, path="/v1/orders", method="GET", host=None):
    """実 ALB が Lambda ターゲットグループへ渡すのと同じ形のイベント"""
    return {
        "requestContext": {"elb": {"targetGroupArn": TARGET_GROUP_ARNS[service]}},
        "httpMethod": method,
        "path": path,
        "queryStringParameters": {},
        "headers": {
            "host": host or "%s.example.com" % service,
            "user-agent": "albcheck/1.0",
            "x-amzn-trace-id": "Root=1-00000000-albcheck000000000000000",
            "x-forwarded-for": "203.0.113.10",
            "x-forwarded-port": "80",
            "x-forwarded-proto": "http",
        },
        "body": "",
        "isBase64Encoded": False,
    }


def check_contract(endpoint, service):
    """自作 Lambda が ALB Lambda ターゲットグループ統合の契約を満たすか検証する"""
    checks = []

    def add(label, expected, actual, ok, level="必須"):
        checks.append(Check(label, expected, actual, ok, level))

    resp = http(endpoint, "POST", {"Content-Type": "application/json"},
                json.dumps(elb_event(service)))
    if resp.error:
        add("invoke", "RIE に接続できること", "エラー: %s" % resp.error, False)
        return checks, resp, None

    add("invoke", "HTTP 200 (RIE)", resp.status, resp.status == 200)
    err = resp.header("X-Amz-Function-Error")
    add("関数エラー", "発生しないこと", err or "なし", not err)

    payload = resp.json()
    add("戻り値の形式", "JSON オブジェクト",
        type(payload).__name__, isinstance(payload, dict))
    if not isinstance(payload, dict):
        return checks, resp, None

    status = payload.get("statusCode")
    add("statusCode", "int (100-599)", repr(status),
        isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599)

    headers = payload.get("headers", {})
    add("headers", "dict[str, str]",
        type(headers).__name__ if not isinstance(headers, dict)
        else "dict(%d 件)" % len(headers),
        isinstance(headers, dict))
    if isinstance(headers, dict):
        bad = [k for k, v in headers.items() if not isinstance(v, str)]
        add("headers の値", "すべて文字列",
            "文字列でない: %s" % ", ".join(bad) if bad else "OK", not bad)

    mvh = payload.get("multiValueHeaders")
    if mvh is not None:
        ok = isinstance(mvh, dict) and all(
            isinstance(v, list) and all(isinstance(x, str) for x in v) for v in mvh.values())
        add("multiValueHeaders", "dict[str, list[str]]", type(mvh).__name__, ok)

    body = payload.get("body", "")
    add("body", "str", type(body).__name__, isinstance(body, str))

    b64 = payload.get("isBase64Encoded", False)
    add("isBase64Encoded", "bool", repr(b64), isinstance(b64, bool))

    ctype = ""
    if isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() == "content-type":
                ctype = str(v)
    kind = "web" if service in WEB_SERVICES else "api"
    if kind == "web":
        add("Content-Type (web 向け)", "text/html*", ctype or "(なし)",
            ctype.lower().startswith("text/html"), level="推奨")
        add("本文 (web 向け)", "HTML であること",
            "HTML" if "<html" in str(body).lower() else "HTML でない",
            "<html" in str(body).lower(), level="推奨")
    else:
        add("Content-Type (api 向け)", "application/json*", ctype or "(なし)",
            ctype.lower().startswith("application/json"), level="推奨")
        add("本文 (api 向け)", "HTML を返さないこと",
            "HTML を含む" if "<html" in str(body).lower() else "OK",
            "<html" not in str(body).lower(), level="推奨")
    add("Retry-After", "設定を推奨",
        headers.get("Retry-After", "(なし)") if isinstance(headers, dict) else "(なし)",
        isinstance(headers, dict) and "Retry-After" in headers, level="推奨")

    return checks, resp, payload


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------
def cmd_check(args):
    service = args.service
    if service not in SERVICES:
        print("unknown service: %s (%s)" % (service, "|".join(SERVICES)), file=sys.stderr)
        return 2
    headers = dict(h.split(":", 1) for h in args.header) if args.header else {}
    headers = {k.strip(): v.strip() for k, v in headers.items()}
    if args.xff:
        headers["X-Forwarded-For"] = args.xff
    case = Case("CHECK", "%s %s%s" % (args.method, alb_url(service), args.path),
                service, args.path, args.method, headers)
    result = do_case(case, "手動確認", args.width, args.renderer)
    print_detail(result, args.width, show_screen=not args.no_screen, show_body=args.raw)

    if args.excel:
        run = Run(_current_variant(service), args.width, args.renderer)
        run.results = [result]
        run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_excel(run, args.excel)
    return 0 if not result.resp.error else 1


def _current_variant(service):
    info = get_lambda_info(service)
    return info.get("variant", "?")


def cmd_report(args):
    variants = []
    if args.variant == "both":
        variants = ["builtin", "custom"]
    elif args.variant == "current":
        variants = [None]
    else:
        variants = [args.variant]

    runs = []
    exit_code = 0
    for variant in variants:
        run = _run_plan(variant, args)
        runs.append(run)
        if run.check_failed:
            exit_code = 1

    if args.contract:
        for run in runs:
            variant = run.variant
            endpoint = lambda_url(variant if variant in ("builtin", "custom") else "builtin")
            for service in SERVICES:
                checks, resp, payload = check_contract(endpoint, service)
                run.contract.append((service, endpoint, checks, resp, payload))
                if any(not c.ok and c.level == "必須" for c in checks):
                    exit_code = 1

    for run in runs:
        _print_summary(run, args.width)

    if not args.no_excel:
        path = args.excel or _default_excel_path()
        _write_excel(runs, path)
    return exit_code


def _run_plan(variant, args):
    run = Run(variant, args.width, args.renderer)
    started = time.perf_counter()

    if variant:
        print(c("\n[Lambda 実装を '%s' に切り替えます]" % variant, "1;33"))
        for service in SERVICES:
            r = set_variant(service, variant)
            if r.error or r.status != 200:
                print(c("  警告: %s の切り替えに失敗 (%s)"
                        % (service, r.error or r.status), "1;31"))
        run.variant = variant
    else:
        run.variant = _current_variant(SERVICES[0])

    for step in PLAN:
        print()
        print(c("=== %s : %s ===" % (step["group"], step["desc"]), "1;36"))
        for service, enabled in step["maintenance"].items():
            set_maintenance(service, enabled)
        time.sleep(0.2)
        for case in step["cases"]:
            result = do_case(case, step["group"], args.width, args.renderer, run.variant)
            run.results.append(result)
            mark = {"PASS": "✔", "FAIL": "✘", "WARN": "▲", "INFO": "・"}[result.verdict]
            print("  %s %-4s %-6s %s" % (
                c(mark, _verdict_color(result.verdict)), result.case.id,
                result.resp.status or "ERR", result.case.title))
            for chk in result.checks:
                if not chk.ok:
                    print("        %s %s  期待=%s / 実際=%s" % (
                        c(chk.verdict, _verdict_color(chk.verdict)),
                        chk.label, chk.expected, chk.actual))
            if args.verbose:
                print_detail(result, args.width)

    # 後片付け: 全 ALB を通常モードへ
    for service in SERVICES:
        set_maintenance(service, False)

    run.elapsed = time.perf_counter() - started
    run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return run


def _print_summary(run, width):
    print()
    print(c("=" * width, "38;5;39"))
    label = "Lambda 実装: %s" % run.variant
    print(" 検証結果  %s   ケース %d 件 / チェック %d 件" % (
        label, len(run.results), run.check_total))
    print("   PASS=%s  FAIL=%s  WARN=%s   (%.1f 秒)" % (
        c(run.passed, "1;32"), c(run.failed, "1;31" if run.failed else "0"),
        c(run.warned, "1;33" if run.warned else "0"), run.elapsed))
    if run.contract:
        ng = sum(1 for _, _, checks, _, _ in run.contract
                 for chk in checks if not chk.ok and chk.level == "必須")
        print("   Lambda 契約チェック: %s" % (c("NG %d 件" % ng, "1;31") if ng
                                             else c("すべて適合", "1;32")))
    print(c("=" * width, "38;5;39"))


def cmd_contract(args):
    endpoint = args.endpoint or lambda_url(args.variant)
    print()
    print(c("Lambda 契約チェック  endpoint=%s" % endpoint, "1;36"))
    results = []
    failed = 0
    for service in SERVICES:
        checks, resp, payload = check_contract(endpoint, service)
        results.append((service, endpoint, checks, resp, payload))
        ng = [chk for chk in checks if not chk.ok and chk.level == "必須"]
        warn = [chk for chk in checks if not chk.ok and chk.level == "推奨"]
        failed += len(ng)
        verdict = "FAIL" if ng else ("WARN" if warn else "PASS")
        print()
        print("  %s %-9s (%s 向け)" % (
            c(verdict, _verdict_color(verdict)), service,
            "web" if service in WEB_SERVICES else "api"))
        for chk in checks:
            mark = {"PASS": "✔", "FAIL": "✘", "WARN": "▲"}[chk.verdict]
            line = "     %s %s 期待=%s" % (
                c(mark, _verdict_color(chk.verdict)),
                T.pad_to_width(chk.label, 26), chk.expected)
            if not chk.ok:
                line += "  / 実際=%s" % chk.actual
            print(line)
        if payload and args.screen:
            headers = payload.get("headers") or {}
            ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
            title, lines, kind = T.render_body(ctype, payload.get("body") or "",
                                               args.width - 4, args.renderer)
            print(T.to_ansi(T.frame(lines, args.width, title=title or ctype,
                                    subtitle="%s (直接 invoke) → %s"
                                             % (service, payload.get("statusCode"))), COLOR))
    print()
    print(c("契約チェック: %s" % ("NG %d 件" % failed if failed else "すべて適合"),
            "1;31" if failed else "1;32"))

    if args.excel:
        run = Run(args.variant, args.width, args.renderer)
        run.contract = results
        run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_excel(run, args.excel)
    return 1 if failed else 0


def cmd_variant(args):
    targets = SERVICES if args.service == "all" else [args.service]
    if args.name in (None, "show"):
        for service in targets:
            info = get_lambda_info(service)
            if not info:
                print("  %-9s 取得できません (ALB が起動していますか)" % service)
                continue
            print("  %-9s variant=%s   選択肢=%s" % (
                service, c(info.get("variant"), "1;33"),
                ", ".join(info.get("available", []))))
            if args.verbose:
                for name, tg in (info.get("target_groups") or {}).items():
                    print("      %s -> %s" % (name, tg.get("effective_endpoint")))
        return 0
    code = 0
    for service in targets:
        resp = set_variant(service, args.name)
        data = resp.json() or {}
        if resp.status == 200:
            print("  %-9s -> lambda variant = %s" % (service, c(data.get("variant"), "1;33")))
        else:
            print("  %-9s %s" % (service, c(data.get("error") or resp.error, "1;31")))
            code = 1
    return code


def cmd_render(args):
    target = args.target
    if target in SERVICES:
        target = alb_url(target) + (args.path or "/")
    if target.startswith("http://") or target.startswith("https://"):
        resp = http(target, headers=dict(
            h.split(":", 1) for h in args.header) if args.header else {})
        if resp.error:
            print(c("接続エラー: %s" % resp.error, "1;31"))
            return 1
        ctype, body, status = resp.content_type, resp.body, resp.status
    else:
        with open(target, "r", encoding="utf-8") as f:
            body = f.read()
        ctype, status = "text/html", "-"
    title, lines, _ = T.render_body(ctype, body, args.width - 4, args.renderer)
    print(T.to_ansi(T.frame(lines, args.width, title=title or ctype,
                            subtitle="%s  →  %s" % (target, status)), COLOR))
    return 0


# ---------------------------------------------------------------------------
def _default_excel_path():
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    if os.environ.get("REPORT_DIR"):
        base = os.environ["REPORT_DIR"]
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "alb-lambda-report_%s.xlsx" % time.strftime("%Y%m%d-%H%M%S"))


def _write_excel(runs, path):
    import report_excel

    runs = runs if isinstance(runs, list) else [runs]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    report_excel.build(runs, path)
    print()
    print(c("Excel レポートを出力しました: %s" % os.path.abspath(path), "1;32"))


def main(argv=None):
    global COLOR
    parser = argparse.ArgumentParser(
        prog="albcheck", description="ALB × Lambda メンテナンス応答の呼び出し確認ツール")
    parser.add_argument("--width", type=int, default=int(os.environ.get("ALBCHECK_WIDTH", "88")),
                        help="表示幅 (既定 88)")
    parser.add_argument("--renderer", default="builtin",
                        choices=["builtin", "auto", "w3m", "lynx", "links"],
                        help="HTML 描画に使うテキストブラウザ (既定 builtin)")
    parser.add_argument("--no-color", action="store_true", help="ANSI 色を使わない")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="1 リクエストのレスポンス詳細と画面表示")
    p.add_argument("service", choices=SERVICES)
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("--method", default="GET")
    p.add_argument("-H", "--header", action="append", help="追加ヘッダ 'Name: value'")
    p.add_argument("--xff", help="X-Forwarded-For (source-ip 条件の検証用)")
    p.add_argument("--raw", action="store_true", help="本文の生データも表示")
    p.add_argument("--no-screen", action="store_true", help="画面表示を省略")
    p.add_argument("--excel", help="この 1 件を Excel に出力")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("report", help="全シナリオを検証して Excel レポートを出力")
    p.add_argument("--variant", default="current",
                   choices=["current", "builtin", "custom", "both"],
                   help="検証する Lambda 実装 (both = 両方を続けて検証)")
    p.add_argument("--excel", help="出力先 xlsx (既定 reports/alb-lambda-report_<日時>.xlsx)")
    p.add_argument("--no-excel", action="store_true", help="Excel を出力しない")
    p.add_argument("--contract", action="store_true", help="Lambda 契約チェックも実施する")
    p.add_argument("-v", "--verbose", action="store_true", help="全ケースの詳細画面も表示")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("contract", help="Lambda が ALB 統合の契約を守っているか検証")
    p.add_argument("--variant", default="custom", help="builtin / custom")
    p.add_argument("--endpoint", help="RIE の invoke URL を直接指定")
    p.add_argument("--screen", action="store_true", help="返ってきた本文を画面表示する")
    p.add_argument("--excel", help="結果を Excel に出力")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("variant", help="invoke 先 Lambda (builtin/custom) の確認・切り替え")
    p.add_argument("name", nargs="?", help="切り替え先 (省略時は現在値を表示)")
    p.add_argument("--service", default="all", choices=SERVICES + ["all"])
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_variant)

    p = sub.add_parser("render", help="URL / HTML ファイルをテキストブラウザ描画")
    p.add_argument("target", help="URL, HTML ファイル, またはサービス名")
    p.add_argument("--path", default="/", help="サービス名を指定したときのパス")
    p.add_argument("-H", "--header", action="append")
    p.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    COLOR = T.enable_ansi() and not args.no_color
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n中断しました", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
