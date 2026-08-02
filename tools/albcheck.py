#!/usr/bin/env python3
"""
albcheck — ALB × Lambda メンテナンス応答の呼び出し確認ツール (標準ライブラリのみ)

    python tools/albcheck.py check    intraweb /dashboard   # 1 リクエストの詳細 + 画面表示
    python tools/albcheck.py report   --excel reports/r.xlsx # 全シナリオ検証 + Excel レポート
    python tools/albcheck.py contract --variant custom       # 自作 Lambda が契約を守っているか
    python tools/albcheck.py variant  custom                 # invoke 先 Lambda を差し替える
    python tools/albcheck.py render   http://localhost:8081/ # 画面をテキストブラウザ表示
    python tools/albcheck.py doctor                          # 繋がらないときの原因診断

pip install は不要 (urllib + 自作 xlsx ライタ)。接続先はすべて環境変数で上書きできる:
    ALB_URL_<SERVICE>     例 ALB_URL_INTRAWEB=http://alb-intraweb
    ADMIN_URL_<SERVICE>   例 ADMIN_URL_INTRAWEB=http://alb-intraweb:9000
    LAMBDA_URL_<VARIANT>  例 LAMBDA_URL_CUSTOM=http://custom-lambda:8080/...

環境変数が無い場合は接続先を自動検出する (ホストの公開ポート → コンテナ名 → コンテナ IP)。
ブラウザを開けない環境 (Session Manager 経由の EC2 など) を想定しているため、GUI は一切不要。
自動検出を止めたいときは --no-autodiscover か ALBCHECK_AUTODISCOVER=0。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import envprobe as P  # noqa: E402
import textrender as T  # noqa: E402

# 終了コード
EXIT_OK = 0
EXIT_FAILED = 1          # 検証項目が期待どおりでない
EXIT_UNREACHABLE = 3     # そもそも検証環境へ接続できない (ラッパがこれを見て再実行する)

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


# ---------------------------------------------------------------------------
# 接続先の自動検出
#
# ブラウザも GUI も無い環境 (Session Manager 経由の EC2 など) では、
# 「localhost:8081 が繋がらない = 何も確認できない」になりがちなので、
# 次の順に候補を試して、繋がったところを自動的に採用する。
#
#   1. 環境変数 (ALB_URL_* / ADMIN_URL_* / LAMBDA_URL_*)  … 明示指定が最優先
#   2. ホストに公開されたポート    127.0.0.1:8081 など     … 通常のホスト実行
#   3. コンテナ名                  http://alb-intraweb     … inspector コンテナの中から
#   4. コンテナ IP                 http://172.x.x.x:80     … Linux + rootful ランタイム
#
# どれも駄目なら doctor で原因を切り分けられるよう、試行結果を残しておく。
# ---------------------------------------------------------------------------
AUTODISCOVER = os.environ.get("ALBCHECK_AUTODISCOVER", "1") != "0"
_ENDPOINTS = {}      # key -> P.Endpoint (採用したもの)
_ATTEMPTS = {}       # key -> [P.Endpoint] (全試行結果)
_ANNOUNCED = set()   # 自動検出の通知を出したキー
_RUNTIME_CACHE = []


def _runtime():
    """コンテナ IP を引くためのランタイム (見つからなければ None)"""
    if not _RUNTIME_CACHE:
        _RUNTIME_CACHE.append(P.primary_runtime())
    return _RUNTIME_CACHE[0]


def _by_container_ip(container, port, path=""):
    """コンテナ IP から URL を組み立てる (前の候補が全滅したときだけ評価される)"""
    def thunk():
        runtime = _runtime()
        if not runtime:
            return ""
        ip = P.container_ip(runtime.name, container)
        return "http://%s:%d%s" % (ip, port, path) if ip else ""
    return thunk


def _endpoint(key, env_name, specs):
    """key ごとに接続先を 1 度だけ解決してキャッシュする"""
    if key in _ENDPOINTS:
        return _ENDPOINTS[key]

    override = os.environ.get(env_name)
    if override:
        endpoint = P.Endpoint(override, "環境変数 %s" % env_name, True, "明示指定")
    elif not AUTODISCOVER:
        source, spec = specs[0]
        endpoint = P.Endpoint(spec() if callable(spec) else spec, source)
        _ATTEMPTS[key] = [endpoint]
    else:
        found, attempts = P.resolve(specs)
        _ATTEMPTS[key] = attempts
        # 全滅した場合も既定候補を返しておく (エラー本文が従来どおりになる)
        endpoint = found or attempts[0]
        if found is None:
            # 1 つも繋がらない環境で report を流すと候補探索だけで何十秒もかかるので、
            # 2 件目以降のタイムアウトは詰める (どうせ同じ結果になる)
            P.PROBE_TIMEOUT = min(P.PROBE_TIMEOUT, 0.3)
        if found is not None and attempts[0] is not found and key not in _ANNOUNCED:
            _ANNOUNCED.add(key)
            print(c("[接続先を自動検出] %s → %s  (%s / %s は %s)"
                    % (key, found.url, found.source,
                       attempts[0].url or "既定候補", attempts[0].detail), "38;5;244"))

    _ATTEMPTS.setdefault(key, [endpoint])
    _ENDPOINTS[key] = endpoint
    return endpoint


def alb_url(service):
    return _endpoint("%s ALB" % service, "ALB_URL_" + service.upper(), [
        ("ホストの公開ポート", "http://127.0.0.1:%d" % _ALB_PORTS[service]),
        ("コンテナ名 (lab ネットワーク内)", "http://alb-%s" % service),
        ("コンテナ IP", _by_container_ip("alb-%s" % service, 80)),
    ]).url


def admin_url(service):
    return _endpoint("%s 管理 API" % service, "ADMIN_URL_" + service.upper(), [
        ("ホストの公開ポート", "http://127.0.0.1:%d" % _ADMIN_PORTS[service]),
        ("コンテナ名 (lab ネットワーク内)", "http://alb-%s:9000" % service),
        ("コンテナ IP", _by_container_ip("alb-%s" % service, 9000)),
    ]).url


def lambda_url(variant):
    container = P.LAMBDA_CONTAINER.get(variant, "maintenance-lambda")
    return _endpoint("%s Lambda (RIE)" % variant, "LAMBDA_URL_" + variant.upper(), [
        ("ホストの公開ポート",
         "http://127.0.0.1:%d%s" % (_LAMBDA_PORTS.get(variant, 9001), RIE_PATH)),
        ("コンテナ名 (lab ネットワーク内)", "http://%s:8080%s" % (container, RIE_PATH)),
        ("コンテナ IP", _by_container_ip(container, 8080, RIE_PATH)),
    ]).url


def unreachable_keys():
    """接続先を 1 つも解決できなかったものの一覧"""
    return [key for key, attempts in _ATTEMPTS.items()
            if attempts and not any(a.ok for a in attempts)]


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
        detail = P.explain_error_text(resp.error)
        add("接続", "接続できること",
            "エラー: %s%s" % (resp.error, ("  / %s" % detail) if detail else ""), False)
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
        print_connection_help(resp.url, resp.error, width)
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


def print_connection_help(url, error, width=78):
    """接続できなかったときに、その場で次の一手が分かる短い診断を出す

    ブラウザで開いて確かめる、という選択肢が無い環境向けなので、
    「何を試して」「なぜ駄目で」「次に何をすればよいか」まで文字で出し切る。
    """
    detail = P.explain_error_text(error)
    if detail:
        print("  " + c("→ " + detail, "33"))

    section("試した接続先", width)
    tried = False
    for key, attempts in _ATTEMPTS.items():
        if any(a.ok for a in attempts):
            continue
        tried = True
        print("  %s" % c(key, "1"))
        for attempt in attempts:
            print("    %s %s %s" % (c("✘", "31"),
                                    T.pad_to_width(attempt.url or
                                                   "[%s]" % attempt.source, 44),
                                    attempt.detail))
    if not tried:
        print("  (自動検出は無効です。--no-autodiscover / ALBCHECK_AUTODISCOVER=0 を確認)")

    section("コンテナの状態", width)
    runtime = _runtime()
    if runtime is None:
        installed = P.runtimes()
        if not installed:
            print("  " + c("コンテナランタイム (docker / podman / nerdctl) が"
                           "見つかりません", "1;31"))
            print("    RHEL では: sudo dnf install -y podman podman-compose")
        else:
            print("  " + c("ランタイムはありますが使える状態ではありません", "1;31"))
            for entry in installed:
                print("    %s: %s" % (entry.name,
                                      T.trim_to_width(entry.reason, width - 10)))
    else:
        table, _ = P.containers(runtime.name)
        down = []
        for name in P.ALL_CONTAINERS:
            row = table.get(name)
            if row is None:
                down.append((name, "存在しません"))
            elif row["state"].lower() != "running":
                down.append((name, row["status"] or row["state"]))
        if not down:
            print("  " + c("10 コンテナすべて running です", "32")
                  + " — ポート公開設定 (ports:) と firewalld を確認してください")
        else:
            print("  " + c("起動していないコンテナ: %d 件" % len(down), "1;31"))
            for name, why in down[:6]:
                print("    %s %-22s %s" % (c("✘", "31"), name, why))
            if len(down) > 6:
                print("    ... ほか %d 件" % (len(down) - 6))
            first = down[0][0]
            logs = P.container_logs(runtime.name, first, 5)
            if logs:
                print("  %s のログ末尾:" % c(first, "1"))
                for line in logs:
                    print("    | " + T.trim_to_width(line, width - 6))

    section("次にやること", width)
    compose = runtime.compose_display if (runtime and runtime.compose) else "docker compose"
    print("  1. 詳しい診断と対処コマンド : %s" % c("./scripts/report.sh doctor", "1;36"))
    print("  2. 起動していなければ       : %s up -d --build" % compose)
    print("  3. コンテナの中から確認     : %s"
          % c("./scripts/report.sh --in-container check intraweb /dashboard", "1;36"))
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

    if args.save and not result.resp.error:
        _save_text(args.save, result.resp.body)
    if args.save_text and not result.resp.error:
        _save_text(args.save_text, "\n".join(
            text for _style, text in T.frame(
                result.screen_lines, args.width,
                title=result.screen_title or result.resp.content_type.split(";")[0],
                subtitle="%s %s  →  %s" % (case.method, result.resp.url,
                                           result.resp.status))) + "\n")

    if args.excel:
        run = Run(_current_variant(service), args.width, args.renderer)
        run.results = [result]
        run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_excel(run, args.excel)
    return EXIT_OK if not result.resp.error else EXIT_UNREACHABLE


def _save_text(path, text):
    """本文 / 画面描画をファイルに残す (scp や sftp で持ち出して見るため)"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(c("保存しました: %s (%d bytes)"
            % (os.path.abspath(path), len(text.encode("utf-8"))), "1;32"))


def _current_variant(service):
    info = get_lambda_info(service)
    return info.get("variant", "?")


def _abort_if_unreachable(args):
    """どの ALB にも届かないなら、65 ケースを空振りさせずに診断を出して止める"""
    if not AUTODISCOVER:
        return None
    urls = [alb_url(service) for service in SERVICES]
    if any(any(a.ok for a in _ATTEMPTS.get("%s ALB" % s, [])) for s in SERVICES):
        return None
    print()
    print(c("どの ALB にも接続できないため検証を中止しました。", "1;31"))
    print_connection_help(urls[0], "connection refused", args.width)
    return EXIT_UNREACHABLE


def cmd_report(args):
    aborted = _abort_if_unreachable(args)
    if aborted is not None:
        return aborted

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

    # 1 件も応答が無い = 検証環境に届いていない。ラッパが別経路で再実行できるよう区別する
    all_results = [r for run in runs for r in run.results]
    if all_results and all(r.resp.error for r in all_results):
        print()
        print(c("検証環境へ 1 件も到達できませんでした。", "1;31"))
        print_connection_help(all_results[0].resp.url, all_results[0].resp.error, args.width)
        return EXIT_UNREACHABLE

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

    if all(resp.error for _s, _e, _c, resp, _p in results):
        print_connection_help(endpoint, results[0][3].error, args.width)
        return EXIT_UNREACHABLE

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
            print_connection_help(target, resp.error, args.width)
            return EXIT_UNREACHABLE
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
# doctor — ブラウザを開けない環境での原因切り分け
# ---------------------------------------------------------------------------
def _probe_targets():
    """(_ATTEMPTS のキー, 疎通確認する URL, メソッド) の一覧"""
    rows = []
    for service in SERVICES:
        rows.append(("%s ALB" % service, alb_url(service) + "/", "GET"))
        rows.append(("%s 管理 API" % service, admin_url(service) + "/admin/state", "GET"))
    for variant in ("builtin", "custom"):
        rows.append(("%s Lambda (RIE)" % variant, lambda_url(variant), "POST"))
    return rows


def _probe(url, method):
    if method == "POST":
        return http(url, "POST", {"Content-Type": "application/json"},
                    json.dumps(elb_event("intraweb")), timeout=8)
    return http(url, timeout=8)


def _quiet_doctor():
    """ラッパ用の高速プリフライト: intraweb に届くかだけを短いタイムアウトで見る"""
    P.PROBE_TIMEOUT = float(os.environ.get("ALBCHECK_PROBE_TIMEOUT", "0.5"))
    url = alb_url("intraweb")
    resp = http(url + "/", timeout=5)
    if not resp.error:
        source = _ENDPOINTS["intraweb ALB"].source
        print("albcheck: 検証環境へ到達できます (%s / %s)" % (url, source), file=sys.stderr)
        return EXIT_OK
    print("albcheck: 検証環境へ到達できません (%s : %s)"
          % (url, P.explain_error_text(resp.error) or resp.error), file=sys.stderr)
    return EXIT_UNREACHABLE


def _label(text, width):
    return T.pad_to_width(text, width)


def cmd_doctor(args):
    width = args.width
    if args.quiet:
        return _quiet_doctor()

    # 先に全部解決しておく (自動検出の通知がセクションの途中に割り込まないように)
    rows = _probe_targets()

    info = P.host_report()
    print()
    print(c("═" * width, "38;5;39"))
    print(" " + c("albcheck doctor — 接続できないときの原因切り分け (GUI 不要)", "1"))
    print(c("═" * width, "38;5;39"))

    section("実行環境", width)
    print("  %s %s" % (_label("OS", 14), info["os"]))
    print("  %s %s" % (_label("Python", 14), info["python"]))
    print("  %s %s" % (_label("実行場所", 14), info["where"]))
    print("  %s %s" % (_label("SELinux", 14), info["selinux"] or "無効 / 非対応"))
    if info["ssm"]:
        print("  %s %s" % (_label("接続経路", 14),
                           "AWS Systems Manager セッション (GUI なし)"))
    print("  %s %s" % (_label("GUI ブラウザ", 14),
                       "あり (%s)" % info["gui_reason"] if info["gui_browser"]
                       else c("なし", "1;33") + " — %s" % info["gui_reason"]))
    print("  %s %s" % (_label("テキスト描画", 14), "builtin (内蔵)" + (
        " / " + ", ".join(info["text_browsers"]) if info["text_browsers"]
        else "   ※ w3m / lynx は未インストール (内蔵描画で確認できます)")))
    if not info["gui_browser"]:
        print("  " + c("→ ブラウザの代わりに check / render のテキスト描画で"
                       "メンテナンス画面を確認できます", "32"))

    section("コンテナランタイム", width)
    runtime_list = P.runtimes()
    if not runtime_list:
        print("  " + c("✘ docker / podman / nerdctl のいずれも見つかりません", "1;31"))
    for runtime in runtime_list:
        mark = c("✔", "1;32") if runtime.usable else c("✘", "1;31")
        print("  %s %-9s %-30s compose: %s"
              % (mark, runtime.name, runtime.version or "-", runtime.compose_display))
        if not runtime.usable:
            print("      %s" % c(T.trim_to_width(runtime.reason, width - 6), "31"))
        elif runtime.rootless:
            print("      rootless モードです "
                  "(コンテナ IP へはホストから直接届きません)")

    runtime = _runtime()
    table = {}
    stopped = []
    section("コンテナの状態", width)
    if runtime is None:
        print("  " + c("使えるランタイムが無いため確認できません", "1;31"))
    else:
        table, result = P.containers(runtime.name)
        if not table:
            print("  " + c("コンテナ一覧を取得できませんでした: %s"
                           % (result.message if result else "?"), "1;31"))
        for name in P.ALL_CONTAINERS:
            row = table.get(name)
            if row is None:
                stopped.append((name, "存在しません"))
                print("  %s %-22s %s" % (c("✘", "1;31"), name, "(存在しません)"))
            elif row["state"].lower() != "running":
                stopped.append((name, row["status"] or row["state"]))
                print("  %s %-22s %s" % (c("✘", "1;31"), name, row["status"]))
            else:
                print("  %s %-22s %-16s %s" % (c("✔", "1;32"), name, row["status"],
                                               T.trim_to_width(row["ports"], width - 46)))
        for name, _why in stopped[:2]:
            logs = P.container_logs(runtime.name, name, 6)
            if logs:
                print("  %s のログ末尾:" % c(name, "1"))
                for line in logs:
                    print("    | " + T.trim_to_width(line, width - 6))

    section("接続確認", width)
    reachable, unreachable = [], []
    for key, url, method in rows:
        resp = _probe(url, method)
        if resp.error:
            unreachable.append(key)
            print("  %s %s %s" % (c("✘", "1;31"), _label(key, 20), url))
            print("      %s" % c(P.explain_error_text(resp.error) or resp.error, "31"))
            for attempt in _ATTEMPTS.get(key, []):
                if not attempt.ok:
                    print("      ・%s %s"
                          % (_label(attempt.url or "[%s]" % attempt.source, 44),
                             attempt.detail))
        else:
            reachable.append(key)
            source = _ENDPOINTS[key].source if key in _ENDPOINTS else ""
            print("  %s %s %s HTTP %s  [%s]"
                  % (c("✔", "1;32"), _label(key, 20),
                     _label(T.trim_to_width(url, 44), 44), resp.status, source))

    section("診断", width)
    compose = runtime.compose_display if (runtime and runtime.compose) else "docker compose"
    if not unreachable:
        print("  " + c("✔ 検証環境へ到達できています。ブラウザ無しで次のとおり確認できます:",
                       "1;32"))
        print("      ./scripts/mctl.sh on intraweb")
        print("      ./scripts/report.sh check intraweb /dashboard    # 画面をテキスト描画")
        print("      ./scripts/report.sh report                       # 全シナリオ + Excel")
    else:
        if runtime is None and not runtime_list:
            print("  " + c("✘ コンテナランタイム (docker / podman / nerdctl) が"
                           "インストールされていません。", "1;31"))
            print("      RHEL 9: sudo dnf install -y podman podman-compose")
            print("      その後: podman compose up -d --build")
        elif runtime is None:
            print("  " + c("✘ ランタイムはありますが使える状態ではありません。", "1;31"))
            for entry in runtime_list:
                print("      %s: %s" % (entry.name,
                                        T.trim_to_width(entry.reason, width - 12)))
            print("      デーモン起動: sudo systemctl start docker  "
                  "(podman なら systemctl --user start podman.socket)")
            print("      権限不足なら: sudo usermod -aG docker $USER   → 再ログイン")
        elif stopped:
            print("  " + c("✘ 起動していないコンテナが %d 件あります。"
                           "これが接続拒否の原因です。" % len(stopped), "1;31"))
            print("      %s up -d --build" % compose)
            print("      %s logs --tail 50 %s" % (runtime.name, stopped[0][0]))
            if info["selinux"] == "Enforcing":
                print("  " + c("・SELinux が Enforcing です。", "1;33")
                      + "バインドマウントが読めずに ALB が起動しない場合は、")
                print("      %s -f docker-compose.yml -f docker-compose.selinux.yml "
                      "up -d --build" % compose)
                print("    (:z 付きでマウントし直すオーバーレイを同梱しています)")
        else:
            print("  " + c("✘ コンテナは動いていますが、このプロセスからは届きません。", "1;31"))
            print("      ・ホストのポート公開を確認 : %s port alb-intraweb" % runtime.name)
            if runtime.rootless:
                print("      ・rootless のためコンテナ IP には直接届きません。"
                      "コンテナの中から実行してください:")
            print("      ・コンテナの中から実行     : "
                  + c("./scripts/report.sh --in-container check intraweb /dashboard", "1;36"))

    section("他のスクリプトにも同じ接続先を使わせる", width)
    print("  # 下記を export すると mctl.sh / verify.sh / report.sh が同じ宛先を使います")
    for service in SERVICES:
        print("  export %-38s %s"
              % ("ALB_URL_%s=%s" % (service.upper(), alb_url(service)),
                 "ADMIN_URL_%s=%s" % (service.upper(), admin_url(service))))
    print()
    return EXIT_OK if not unreachable else EXIT_UNREACHABLE


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
    global COLOR, AUTODISCOVER
    parser = argparse.ArgumentParser(
        prog="albcheck", description="ALB × Lambda メンテナンス応答の呼び出し確認ツール")
    parser.add_argument("--width", type=int, default=int(os.environ.get("ALBCHECK_WIDTH", "88")),
                        help="表示幅 (既定 88)")
    parser.add_argument("--renderer", default="builtin",
                        choices=["builtin", "auto", "w3m", "lynx", "links"],
                        help="HTML 描画に使うテキストブラウザ (既定 builtin)")
    parser.add_argument("--no-color", action="store_true", help="ANSI 色を使わない")
    parser.add_argument("--no-autodiscover", action="store_true",
                        help="接続先の自動検出をせず localhost だけを使う")
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
    p.add_argument("--save", metavar="FILE",
                   help="レスポンス本文をそのまま保存 (例 screen.html)")
    p.add_argument("--save-text", metavar="FILE",
                   help="画面のテキスト描画結果を保存 (例 screen.txt)")
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

    p = sub.add_parser("doctor", help="接続できないときの原因切り分け (GUI 不要)")
    p.add_argument("--quiet", action="store_true",
                   help="1 行だけ表示して終了コードで返す (ラッパ用)")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    COLOR = T.enable_ansi() and not args.no_color
    if args.no_autodiscover:
        AUTODISCOVER = False
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n中断しました", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
