"""
ALB (Application Load Balancer) シミュレータ

1 コンテナ = 1 ALB。YAML で定義したリスナールールを優先度順に評価し、
  - target group type: ecs    -> 後続の ECS サービス(HTTP)へプロキシ
  - target group type: lambda -> Lambda 関数(RIE)を ELB イベント形式で invoke
を行う。実 ALB の Lambda ターゲットグループ統合と同じイベント/レスポンス仕様を再現する。

ポート:
  LISTENER_PORT (default 80)  : 本番トラフィック用リスナー
  ADMIN_PORT    (default 9000): メンテナンスモード切替などの管理 API
"""

import fnmatch
import ipaddress
import json
import logging
import os
import sys
import threading
import time
import uuid

import requests
import yaml
from flask import Flask, Response, jsonify, request
from waitress import serve

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("ALB_CONFIG", "/etc/alb/listener-rules.yaml")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
LISTENER_PORT = int(os.environ.get("LISTENER_PORT", "80"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "9000"))
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "10"))

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("alb")

# ホップバイホップヘッダ (プロキシ時に転送しない)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


class Config:
    """YAML 設定 + メンテナンス状態を保持する。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.load()

    def load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        with self.lock:
            self.raw = raw
            self.alb_name = raw["alb"]["name"]
            self.service = raw["alb"]["service"]
            self.target_groups = raw["target_groups"]
            self.default_action = raw["listener"]["default_action"]
            self.rules = sorted(raw.get("rules", []), key=lambda r: r["priority"])
        log.info(
            "[%s] loaded config: %d rules, %d target groups",
            self.alb_name,
            len(self.rules),
            len(self.target_groups),
        )

    # ---- メンテナンス状態 (ファイル永続化) ----
    @property
    def state_file(self):
        return os.path.join(STATE_DIR, f"maintenance-{self.service}.json")

    def get_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            default = os.environ.get("MAINTENANCE_DEFAULT", "false").lower() == "true"
            state = {"maintenance": default, "updated_at": None, "note": ""}
            self.set_state(state["maintenance"], state["note"])
            return state

    def set_state(self, enabled, note=""):
        state = {
            "maintenance": bool(enabled),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": note,
        }
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, self.state_file)
        log.info("[%s] maintenance -> %s (%s)", self.alb_name, state["maintenance"], note)
        return state

    def maintenance_on(self):
        return self.get_state()["maintenance"]


cfg = Config(CONFIG_PATH)


# --------------------------------------------------------------------------
# リスナールール評価
# --------------------------------------------------------------------------
def client_ip():
    """実 ALB の source-ip 条件は「ALB に接続してきたクライアント IP」を見る。
    ローカル検証では X-Forwarded-For で任意の IP を詐称できるようにする。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def match_condition(cond):
    field = cond["field"]
    values = cond.get("values", [])

    if field == "path-pattern":
        path = request.path
        return any(fnmatch.fnmatchcase(path, v) for v in values)

    if field == "host-header":
        host = (request.headers.get("Host") or "").split(":")[0].lower()
        return any(fnmatch.fnmatchcase(host, v.lower()) for v in values)

    if field == "http-request-method":
        return request.method.upper() in [v.upper() for v in values]

    if field == "source-ip":
        try:
            ip = ipaddress.ip_address(client_ip())
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(v, strict=False) for v in values)

    if field == "http-header":
        name = cond["http_header_name"]
        actual = request.headers.get(name)
        if actual is None:
            return False
        return any(fnmatch.fnmatchcase(actual, v) for v in values)

    if field == "query-string":
        for kv in values:
            k, v = kv.get("key"), kv.get("value")
            for qk, qv in request.args.items(multi=True):
                if (k is None or fnmatch.fnmatchcase(qk, k)) and fnmatch.fnmatchcase(qv, v):
                    return True
        return False

    log.warning("unknown condition field: %s", field)
    return False


def select_rule():
    """優先度順に最初にマッチしたルールを返す。マッチ無しなら default action。"""
    maint = cfg.maintenance_on()
    for rule in cfg.rules:
        # maintenance: true のルールは「メンテ中だけ差し込まれるリスナールール」
        if rule.get("maintenance") and not maint:
            continue
        if rule.get("enabled") is False:
            continue
        conditions = rule.get("conditions", [])
        if all(match_condition(c) for c in conditions):
            return rule, rule["action"]
    return {"name": "default", "priority": "default"}, cfg.default_action


# --------------------------------------------------------------------------
# アクション実行
# --------------------------------------------------------------------------
def forward_to_ecs(tg, trace_id):
    url = tg["endpoint"].rstrip("/") + request.full_path.rstrip("?")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["X-Forwarded-For"] = client_ip()
    headers["X-Forwarded-Proto"] = "http"
    headers["X-Forwarded-Port"] = str(LISTENER_PORT)
    headers["X-Amzn-Trace-Id"] = trace_id
    try:
        r = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            timeout=BACKEND_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        log.error("backend error: %s", e)
        return Response(
            "<html><body><h1>502 Bad Gateway</h1></body></html>",
            status=502,
            content_type="text/html",
        )
    out = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(r.content, status=r.status_code, headers=out)


def build_elb_event(tg, trace_id):
    """実 ALB が Lambda ターゲットグループへ渡すイベント形式を再現する。"""
    body = request.get_data()
    try:
        body_str, is_b64 = body.decode("utf-8"), False
    except UnicodeDecodeError:
        import base64

        body_str, is_b64 = base64.b64encode(body).decode("ascii"), True

    headers = {k.lower(): v for k, v in request.headers.items()}
    headers["x-forwarded-for"] = client_ip()
    headers["x-forwarded-proto"] = "http"
    headers["x-forwarded-port"] = str(LISTENER_PORT)
    headers["x-amzn-trace-id"] = trace_id

    return {
        "requestContext": {"elb": {"targetGroupArn": tg["target_group_arn"]}},
        "httpMethod": request.method,
        "path": request.path,
        "queryStringParameters": {k: v for k, v in request.args.items()},
        "headers": headers,
        "body": body_str,
        "isBase64Encoded": is_b64,
    }


def invoke_lambda(tg, trace_id):
    event = build_elb_event(tg, trace_id)
    try:
        r = requests.post(tg["endpoint"], json=event, timeout=BACKEND_TIMEOUT)
    except requests.RequestException as e:
        log.error("lambda invoke error: %s", e)
        return alb_502("Lambda invoke failed")

    if r.headers.get("X-Amz-Function-Error"):
        log.error("lambda function error: %s", r.text[:500])
        return alb_502("Lambda function error")

    try:
        payload = r.json()
    except ValueError:
        return alb_502("Lambda returned non-JSON")

    if not isinstance(payload, dict) or "statusCode" not in payload:
        # 実 ALB も Lambda レスポンス形式が不正なら 502 を返す
        return alb_502("Invalid Lambda response format")

    status = int(payload["statusCode"])
    headers = {}
    for k, v in (payload.get("headers") or {}).items():
        headers[k] = v
    for k, vals in (payload.get("multiValueHeaders") or {}).items():
        for v in vals:
            headers[k] = v

    body = payload.get("body", "") or ""
    if payload.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body)
    elif isinstance(body, str):
        body = body.encode("utf-8")

    headers.pop("Content-Length", None)
    headers.pop("content-length", None)
    return Response(body, status=status, headers=headers)


def alb_502(reason):
    log.error("ALB 502: %s", reason)
    return Response(
        "<html><head><title>502 Bad Gateway</title></head>"
        "<body><center><h1>502 Bad Gateway</h1></center></body></html>",
        status=502,
        content_type="text/html",
    )


def fixed_response(action):
    cfgr = action["fixed_response_config"]
    return Response(
        cfgr.get("message_body", ""),
        status=int(cfgr.get("status_code", 503)),
        content_type=cfgr.get("content_type", "text/plain"),
    )


def redirect_response(action):
    c = action["redirect_config"]
    host = c.get("host", request.host.split(":")[0])
    path = c.get("path", request.path)
    proto = c.get("protocol", "http").lower()
    port = c.get("port")
    netloc = host if not port else f"{host}:{port}"
    return Response(
        "",
        status=int(c.get("status_code", "HTTP_302").replace("HTTP_", "")),
        headers={"Location": f"{proto}://{netloc}{path}"},
    )


# --------------------------------------------------------------------------
# リスナー (トラフィック用アプリ)
# --------------------------------------------------------------------------
listener_app = Flask("listener")


@listener_app.route(
    "/", defaults={"_path": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
@listener_app.route(
    "/<path:_path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
def handle(_path):
    started = time.time()
    trace_id = f"Root=1-{int(time.time()):x}-{uuid.uuid4().hex[:24]}"
    rule, action = select_rule()

    atype = action["type"]
    if atype == "forward":
        tg_name = action["target_group"]
        tg = cfg.target_groups[tg_name]
        if tg["type"] == "lambda":
            resp = invoke_lambda(tg, trace_id)
        else:
            resp = forward_to_ecs(tg, trace_id)
        target_desc = f"{tg_name}({tg['type']})"
    elif atype == "fixed-response":
        resp = fixed_response(action)
        target_desc = "fixed-response"
    elif atype == "redirect":
        resp = redirect_response(action)
        target_desc = "redirect"
    else:
        resp = alb_502(f"unsupported action {atype}")
        target_desc = "unsupported"

    resp.headers["X-Amzn-Trace-Id"] = trace_id
    # 検証しやすいように、どのルールでどこへ流れたかをレスポンスヘッダにも出す
    resp.headers["X-Alb-Name"] = cfg.alb_name
    resp.headers["X-Alb-Matched-Rule"] = rule["name"]
    resp.headers["X-Alb-Rule-Priority"] = str(rule["priority"])
    resp.headers["X-Alb-Target"] = target_desc
    resp.headers["X-Alb-Maintenance"] = str(cfg.maintenance_on()).lower()

    log.info(
        'ALB=%s maint=%s client=%s "%s %s" rule=%s(prio=%s) target=%s status=%s %.1fms',
        cfg.alb_name,
        cfg.maintenance_on(),
        client_ip(),
        request.method,
        request.full_path.rstrip("?"),
        rule["name"],
        rule["priority"],
        target_desc,
        resp.status_code,
        (time.time() - started) * 1000,
    )
    return resp


# --------------------------------------------------------------------------
# 管理 API
# --------------------------------------------------------------------------
admin_app = Flask("admin")


@admin_app.get("/admin/state")
def admin_state():
    st = cfg.get_state()
    return jsonify(
        {
            "alb": cfg.alb_name,
            "service": cfg.service,
            "maintenance": st["maintenance"],
            "updated_at": st["updated_at"],
            "note": st["note"],
        }
    )


@admin_app.post("/admin/maintenance")
def admin_set():
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"error": "body must be {\"enabled\": true|false}"}), 400
    st = cfg.set_state(bool(data["enabled"]), data.get("note", ""))
    return jsonify({"alb": cfg.alb_name, "service": cfg.service, **st})


@admin_app.post("/admin/maintenance/on")
def admin_on():
    return jsonify({"alb": cfg.alb_name, **cfg.set_state(True, "admin on")})


@admin_app.post("/admin/maintenance/off")
def admin_off():
    return jsonify({"alb": cfg.alb_name, **cfg.set_state(False, "admin off")})


@admin_app.get("/admin/rules")
def admin_rules():
    maint = cfg.maintenance_on()
    rules = []
    for r in cfg.rules:
        rules.append(
            {
                "priority": r["priority"],
                "name": r["name"],
                "conditions": r.get("conditions", []),
                "action": r["action"],
                "maintenance_only": bool(r.get("maintenance")),
                "active_now": not (r.get("maintenance") and not maint)
                and r.get("enabled") is not False,
            }
        )
    return jsonify(
        {
            "alb": cfg.alb_name,
            "maintenance": maint,
            "rules": rules,
            "default_action": cfg.default_action,
            "target_groups": cfg.target_groups,
        }
    )


@admin_app.post("/admin/reload")
def admin_reload():
    cfg.load()
    return jsonify({"alb": cfg.alb_name, "reloaded": True, "rules": len(cfg.rules)})


# --------------------------------------------------------------------------
def main():
    threading.Thread(
        target=lambda: serve(
            admin_app, host="0.0.0.0", port=ADMIN_PORT, threads=4,
            clear_untrusted_proxy_headers=False,
        ),
        daemon=True,
    ).start()
    log.info(
        "[%s] listener :%s / admin :%s (service=%s)",
        cfg.alb_name,
        LISTENER_PORT,
        ADMIN_PORT,
        cfg.service,
    )
    # clear_untrusted_proxy_headers=False:
    #   waitress は既定で X-Forwarded-* を削除するため、source-ip 条件の検証用に無効化する
    serve(
        listener_app, host="0.0.0.0", port=LISTENER_PORT, threads=16,
        clear_untrusted_proxy_headers=False,
    )


if __name__ == "__main__":
    main()
