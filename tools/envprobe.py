#!/usr/bin/env python3
"""
envprobe — 実行環境とコンテナの状態を調べ、接続先を自動解決する (標準ライブラリのみ)

GUI ブラウザを開けない環境 (Session Manager 経由の EC2 / CI / コンテナの中) でも、
CLI だけで「どこへ繋げばよいか」「なぜ繋がらないか」を判断できるようにするための補助。

albcheck からは主に次を使う:

    resolve(specs)        … 接続先候補を順に TCP 接続し、最初に生きているものを採用する
    explain_error_text()  … urllib の英語エラー文字列に日本語の解説を付ける
    runtimes()            … docker / podman / nerdctl の利用可否と compose コマンド
    containers(rt)        … 検証環境コンテナの状態 (停止していればログ末尾も取れる)
    host_report()         … OS / Python / SELinux / GUI ブラウザ有無

このモジュール単体でも実行できる:

    python tools/envprobe.py        # 環境サマリを 1 画面で表示
"""

import errno
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.parse

# 接続確認の 1 候補あたりのタイムアウト (秒)
PROBE_TIMEOUT = float(os.environ.get("ALBCHECK_PROBE_TIMEOUT", "1.5"))

RUNTIME_NAMES = ("docker", "podman", "nerdctl")

# docker-compose.yml の container_name と一致させること
SERVICES = ("intraweb", "interapi", "intraapi", "sfapi")
ALB_CONTAINER = {s: "alb-" + s for s in SERVICES}
ECS_CONTAINER = {s: "ecs-" + s for s in SERVICES}
LAMBDA_CONTAINER = {"builtin": "maintenance-lambda", "custom": "custom-lambda"}
ALL_CONTAINERS = (
    [ALB_CONTAINER[s] for s in SERVICES]
    + [ECS_CONTAINER[s] for s in SERVICES]
    + [LAMBDA_CONTAINER["builtin"], LAMBDA_CONTAINER["custom"]]
)


# ---------------------------------------------------------------------------
# 外部コマンド実行
# ---------------------------------------------------------------------------
class Cmd(object):
    def __init__(self, argv, code, out, err):
        self.argv = argv
        self.code = code
        self.out = out
        self.err = err

    @property
    def ok(self):
        return self.code == 0

    @property
    def message(self):
        text = (self.err or self.out).strip()
        return text.splitlines()[0] if text else ""


def run(argv, timeout=20):
    """外部コマンドを実行する。失敗しても例外は投げず Cmd を返す。"""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return Cmd(argv, 127, "", str(e))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return Cmd(argv, 124, "", "タイムアウト (%s 秒)" % timeout)
    return Cmd(argv, proc.returncode,
               out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# TCP 到達性
# ---------------------------------------------------------------------------
_ERRNO_JA = {
    errno.ECONNREFUSED: "接続拒否 — そのポートで待ち受けているプロセスがありません",
    errno.EHOSTUNREACH: "ホストに到達できません",
    errno.ENETUNREACH: "ネットワークに到達できません",
    errno.ETIMEDOUT: "接続タイムアウト",
    errno.EACCES: "接続が許可されていません",
    errno.EPERM: "接続が許可されていません",
}


# Windows (WinSock) は errno が別体系なので個別に対応させる
_WSA_JA = {
    10061: _ERRNO_JA[errno.ECONNREFUSED],
    10060: "接続タイムアウト",
    10065: "ホストに到達できません",
    10051: "ネットワークに到達できません",
    10013: "接続が許可されていません",
}


def explain_oserror(e):
    if isinstance(e, socket.timeout):
        return "接続タイムアウト"
    num = getattr(e, "errno", None)
    if num in _ERRNO_JA:
        return "%s [Errno %d]" % (_ERRNO_JA[num], num)
    win = getattr(e, "winerror", None)
    if win in _WSA_JA:
        return "%s [WinError %d]" % (_WSA_JA[win], win)
    if isinstance(e, ConnectionRefusedError):
        return _ERRNO_JA[errno.ECONNREFUSED]
    return str(e)


def explain_error_text(text):
    """urllib が返す英語のエラー文字列に日本語の解説を付ける (該当なしなら空文字)"""
    low = (text or "").lower()
    if "errno 111" in low or "connection refused" in low or "10061" in low:
        return "接続拒否 — 指定のアドレス:ポートで待ち受けているプロセスがありません"
    if ("errno -2" in low or "errno -3" in low or "name or service not known" in low
            or "nodename nor servname" in low or "getaddrinfo" in low):
        return "名前解決に失敗 — コンテナ名はコンテナの外からは解決できません"
    if "errno 113" in low or "no route to host" in low:
        return "経路なし — ルーティングまたはファイアウォールで遮断されています"
    if "timed out" in low or "timeout" in low:
        return "タイムアウト — 経路はあるが応答がありません (firewalld / SG を確認)"
    if "errno 13" in low or "permission denied" in low:
        return "権限エラー — SELinux / firewalld / rootless の制限を確認してください"
    return ""


def split_hostport(url):
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        return None, None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port


def probe_url(url, timeout=None):
    """URL のホスト:ポートへ TCP 接続だけ試す。(成否, 詳細) を返す。"""
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    host, port = split_hostport(url)
    if host is None:
        return False, "URL を解釈できません"
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, "名前解決できません (%s)" % (e.strerror or e)
    detail = "候補アドレスなし"
    for family, stype, proto, _canon, addr in infos:
        sock = socket.socket(family, stype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return True, "%s:%s" % (addr[0], addr[1])
        except OSError as e:
            detail = "%s (%s:%s)" % (explain_oserror(e), addr[0], addr[1])
        finally:
            sock.close()
    return False, detail


class Endpoint(object):
    """接続先候補 1 つ分。source は「どうやって見つけたか」の説明。"""

    def __init__(self, url, source, ok=None, detail=""):
        self.url = (url or "").rstrip("/")
        self.source = source
        self.ok = ok
        self.detail = detail

    def __str__(self):
        return "%s (%s)" % (self.url or "-", self.source)


def resolve(specs, timeout=None):
    """接続先を自動解決する。

    specs は [(説明, URL または URL を返す関数), ...]。先頭から順に TCP 接続を試し、
    最初に繋がった Endpoint と、全試行結果のリストを返す。どれも繋がらなければ
    (None, 試行結果)。URL を返す関数は、前の候補が全滅した場合にだけ評価されるので、
    コンテナ IP の取得のような重い処理を後ろに置いてよい。
    """
    attempts = []
    for source, spec in specs:
        try:
            url = spec() if callable(spec) else spec
        except Exception as e:                      # 検出処理の失敗で全体を止めない
            attempts.append(Endpoint("", source, False, "検出に失敗 (%s)" % e))
            continue
        if not url:
            attempts.append(Endpoint("", source,
                                     False, "URL を特定できません (未起動 / 情報を取得不可)"))
            continue
        ok, detail = probe_url(url, timeout)
        endpoint = Endpoint(url, source, ok, detail)
        attempts.append(endpoint)
        if ok:
            return endpoint, attempts
    return None, attempts


# ---------------------------------------------------------------------------
# コンテナランタイム
# ---------------------------------------------------------------------------
class Runtime(object):
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.version = ""
        self.usable = False
        self.reason = ""
        self.rootless = False
        self.compose = None       # 例: ["docker", "compose"] / ["podman-compose"]

    @property
    def compose_display(self):
        return " ".join(self.compose) if self.compose else "(なし)"


_RUNTIME_CACHE = []


def runtimes(refresh=False):
    """インストールされているコンテナランタイムを調べる (結果はキャッシュ)"""
    if _RUNTIME_CACHE and not refresh:
        return _RUNTIME_CACHE[0]
    found = []
    for name in RUNTIME_NAMES:
        path = shutil.which(name)
        if not path:
            continue
        rt = Runtime(name, path)
        ver = run([name, "--version"], timeout=10)
        if ver.ok and ver.out.strip():
            rt.version = ver.out.strip().splitlines()[0]
        info = run([name, "info"], timeout=30)
        rt.usable = info.ok
        if not info.ok:
            rt.reason = info.message or "info コマンドが失敗しました (権限 / デーモン未起動)"
        else:
            for args in (["info", "--format", "{{.Host.Security.Rootless}}"],
                         ["info", "--format", "{{.SecurityOptions}}"]):
                out = run([name] + args, timeout=15)
                if out.ok and ("true" in out.out.lower() or "rootless" in out.out.lower()):
                    rt.rootless = True
                    break
            if run([name, "compose", "version"], timeout=20).ok:
                rt.compose = [name, "compose"]
            elif shutil.which(name + "-compose"):
                rt.compose = [name + "-compose"]
            elif name == "docker" and shutil.which("docker-compose"):
                rt.compose = ["docker-compose"]
        found.append(rt)
    del _RUNTIME_CACHE[:]
    _RUNTIME_CACHE.append(found)
    return found


def primary_runtime():
    """コンテナ情報の取得に使える最初のランタイム (無ければ None)"""
    for rt in runtimes():
        if rt.usable:
            return rt
    return None


_PS_FORMATS = (
    "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Ports}}",
    "{{.Names}}\t{{.Status}}\t{{.Ports}}",          # .State 非対応の古い実装向け
)


def containers(rt_name):
    """{コンテナ名: {state, status, ports}} と、実行したコマンド結果を返す"""
    last = None
    for fmt in _PS_FORMATS:
        result = run([rt_name, "ps", "--all", "--format", fmt], timeout=30)
        last = result
        if not result.ok:
            continue
        table = {}
        for line in result.out.splitlines():
            cols = line.split("\t")
            name = cols[0].strip().strip("[]").split(",")[0].strip()
            if not name:
                continue
            if len(cols) >= 4:
                state, status, ports = cols[1].strip(), cols[2].strip(), cols[3].strip()
            else:
                status = cols[1].strip() if len(cols) > 1 else ""
                ports = cols[2].strip() if len(cols) > 2 else ""
                state = "running" if status.lower().startswith("up") else "exited"
            table[name] = {"state": state, "status": status, "ports": ports}
        return table, result
    return {}, last


def container_ip(rt_name, container):
    """コンテナの IP アドレス (取れなければ空文字)"""
    for tpl in ("{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                "{{.NetworkSettings.IPAddress}}"):
        result = run([rt_name, "inspect", "-f", tpl, container], timeout=15)
        if not result.ok:
            continue
        for token in result.out.split():
            if token and token not in ("<no", "value>", "<nil>", "<no value>"):
                return token
    return ""


def container_network(rt_name, container):
    """コンテナが繋がっているネットワーク名 (取れなければ空文字)"""
    result = run([rt_name, "inspect", "-f",
                  "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                  container], timeout=15)
    if result.ok:
        for token in result.out.split():
            if token and not token.startswith("<"):
                return token
    return ""


def container_logs(rt_name, container, lines=8):
    """コンテナログの末尾 (起動に失敗した理由を掴むため)"""
    result = run([rt_name, "logs", "--tail", str(lines), container], timeout=20)
    text = (result.out + result.err).strip()
    return text.splitlines()[-lines:] if text else []


# ---------------------------------------------------------------------------
# ホスト環境
# ---------------------------------------------------------------------------
def in_container():
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
    except OSError:
        return False
    return any(key in data for key in ("docker", "containerd", "libpod", "kubepods"))


def selinux_mode():
    """Enforcing / Permissive / 空文字 (SELinux 無効 or 非対応 OS)"""
    try:
        with open("/sys/fs/selinux/enforce", "r") as f:
            return "Enforcing" if f.read().strip() == "1" else "Permissive"
    except OSError:
        return ""


def os_display_name():
    try:
        info = {}
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    info[key] = value.strip('"')
        name = info.get("PRETTY_NAME")
        if name:
            return "%s (kernel %s)" % (name, platform.release())
    except OSError:
        pass
    return platform.platform()


def gui_browser():
    """GUI ブラウザを開ける環境かどうか。(可否, 理由) を返す。"""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True, "OS 標準のブラウザが使えます"
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not display:
        return False, "DISPLAY / WAYLAND_DISPLAY が未設定 (GUI なしの端末)"
    for name in ("xdg-open", "firefox", "google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return True, "DISPLAY=%s / %s あり" % (display, name)
    return False, "DISPLAY はあるがブラウザが見つかりません"


def text_browsers():
    """インストール済みのテキストブラウザ"""
    return [name for name in ("w3m", "lynx", "links") if shutil.which(name)]


def host_report():
    ok, reason = gui_browser()
    return {
        "os": os_display_name(),
        "python": "%s (%s)" % (platform.python_version(), sys.executable),
        "where": "コンテナの中" if in_container() else "ホスト (コンテナ外)",
        "selinux": selinux_mode(),
        "gui_browser": ok,
        "gui_reason": reason,
        "text_browsers": text_browsers(),
        "ssm": bool(os.environ.get("AWS_SSM_SESSION_ID")
                    or os.path.isdir("/var/lib/amazon/ssm")),
    }


# ---------------------------------------------------------------------------
def main():
    info = host_report()
    print("OS            : %s" % info["os"])
    print("Python        : %s" % info["python"])
    print("実行場所      : %s" % info["where"])
    print("SELinux       : %s" % (info["selinux"] or "無効 / 非対応"))
    print("GUI ブラウザ  : %s (%s)" % ("あり" if info["gui_browser"] else "なし",
                                       info["gui_reason"]))
    print("テキスト描画  : builtin%s" % ("" if not info["text_browsers"]
                                         else " / " + ", ".join(info["text_browsers"])))
    for rt in runtimes():
        print("ランタイム    : %-8s %-28s compose=%s%s"
              % (rt.name, rt.version or "-", rt.compose_display,
                 "" if rt.usable else "  ** 使えません: %s" % rt.reason))
    rt = primary_runtime()
    if rt:
        table, _ = containers(rt.name)
        for name in ALL_CONTAINERS:
            row = table.get(name)
            print("  %-22s %s" % (name, row["status"] if row else "(存在しません)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
