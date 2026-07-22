"""
ECS サービスのモック (intraweb / interapi / intraapi / sfapi 共通イメージ)

SERVICE_NAME / SERVICE_KIND 環境変数で振る舞いを変える。
  kind=web -> HTML を返す
  kind=api -> JSON を返す
"""

import json
import logging
import os
import sys

from flask import Flask, Response, jsonify, request
from waitress import serve

SERVICE_NAME = os.environ.get("SERVICE_NAME", "unknown")
SERVICE_KIND = os.environ.get("SERVICE_KIND", "api")
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(SERVICE_NAME)

app = Flask(SERVICE_NAME)


@app.get("/healthz")
def healthz():
    return jsonify({"status": "healthy", "service": SERVICE_NAME})


@app.route("/", defaults={"path": ""},
           methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@app.route("/<path:path>",
           methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def catch_all(path):
    log.info('%s "%s /%s" xff=%s', SERVICE_NAME, request.method, path,
             request.headers.get("X-Forwarded-For"))
    headers = {"X-Backend-Service": SERVICE_NAME, "X-Backend-Kind": SERVICE_KIND}

    if SERVICE_KIND == "web":
        html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>{SERVICE_NAME} - 通常稼働中</title>
<style>body{{font-family:sans-serif;margin:60px;color:#1f2933}}
.badge{{background:#0f9d58;color:#fff;padding:4px 10px;border-radius:999px;font-size:.8rem}}
pre{{background:#f0f2f5;padding:12px;border-radius:8px}}</style></head>
<body>
<p><span class="badge">通常稼働中</span></p>
<h1>{SERVICE_NAME} (ECS service)</h1>
<p>このページは ALB から ECS サービスへ正常にフォワードされたことを示します。</p>
<pre>path   : /{path}
method : {request.method}
served : ECS container ({SERVICE_NAME})</pre>
</body></html>"""
        return Response(html, status=200, headers={**headers, "Content-Type": "text/html; charset=utf-8"})

    body = {
        "status": "ok",
        "service": SERVICE_NAME,
        "kind": SERVICE_KIND,
        "path": "/" + path,
        "method": request.method,
        "served_by": "ecs-container",
        "received_headers": {
            "x-forwarded-for": request.headers.get("X-Forwarded-For"),
            "x-amzn-trace-id": request.headers.get("X-Amzn-Trace-Id"),
        },
    }
    return Response(json.dumps(body, ensure_ascii=False), status=200,
                    headers={**headers, "Content-Type": "application/json; charset=utf-8"})


if __name__ == "__main__":
    log.info("starting %s (kind=%s) on :%s", SERVICE_NAME, SERVICE_KIND, PORT)
    # ALB から渡される X-Forwarded-* を waitress が削除しないようにする
    serve(app, host="0.0.0.0", port=PORT, threads=8, clear_untrusted_proxy_headers=False)
