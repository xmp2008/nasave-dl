#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nasave-dl 订阅旁路服务 (sidecar)

为什么存在：官方后端 POST /api/subs 对免费版硬编码 403（限 1 个订阅），
前端 UI 由本项目 overlay 解锁；本服务提供旁路创建/删除接口，
直接写入共享的 SQLite 数据库（WAL），其余操作（列表/编辑/同步/开关）
全部仍走官方 API（实测无门槛）。创建成功后回调官方 /api/subs/{id}/sync
触发首次同步，失败不阻塞创建。

纯 Python 标准库，无第三方依赖。
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("NASAVE_DB", "/app/data/nasave-dl.db")
MAIN_APP = os.environ.get("NASAVE_MAIN", "http://127.0.0.1:8888")
LISTEN_PORT = int(os.environ.get("BYPASS_PORT", "8890"))
API_TOKEN = os.environ.get("BYPASS_TOKEN", "")  # 非空时要求 X-Bypass-Token 匹配

ALLOWED_STATUS = ("active", "paused", "disabled")
URL_RE = re.compile(r"^https?://\S+$", re.I)


def get_conn():
    # timeout: WAL 模式下与官方进程并发写入时的锁等待
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def trigger_first_sync(sub_id):
    """创建成功后通知官方引擎立即拉一次，失败静默（定时器稍后也会跑）。"""
    try:
        req = urllib.request.Request(
            MAIN_APP + "/api/subs/%d/sync" % sub_id, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def create_sub(data):
    """返回 (http_code, payload)。纯业务逻辑，便于自测。"""
    if not isinstance(data, dict):
        return 400, {"code": 400, "msg": "参数错误"}

    url = str(data.get("url") or "").strip()
    name = str(data.get("name") or "").strip()
    try:
        tpl_id = int(data.get("tpl_id"))
    except (TypeError, ValueError):
        return 400, {"code": 400, "msg": "tpl_id 无效"}
    try:
        interval = int(data.get("interval") or 30)
    except (TypeError, ValueError):
        interval = 30
    status = str(data.get("status") or "active").strip().lower()
    if status not in ALLOWED_STATUS:
        status = "active"

    if not URL_RE.match(url) or len(url) > 2048:
        return 400, {"code": 400, "msg": "订阅链接必须是合法的 http(s) URL"}
    if not name or len(name) > 200:
        return 400, {"code": 400, "msg": "订阅名称必填且不超过 200 字"}

    conn = get_conn()
    try:
        cur = conn.execute(
            "insert into subscriptions(url,name,tpl_id,interval,last_sync,status,created_at)"
            " values(?,?,?,?,null,?,datetime('now'))",
            (url, name, tpl_id, interval, status))
        conn.commit()
        sub_id = int(cur.lastrowid)
    except sqlite3.IntegrityError as e:
        return 409, {"code": 409, "msg": "订阅已存在或数据冲突: %s" % e}
    except sqlite3.OperationalError as e:
        return 503, {"code": 503, "msg": "数据库忙，稍后重试: %s" % e}
    finally:
        conn.close()

    if status == "active":
        trigger_first_sync(sub_id)
    return 200, {"code": 0, "msg": "创建成功", "data": {"id": sub_id}}


def delete_sub(sub_id):
    """返回 (http_code, payload)。"""
    conn = get_conn()
    try:
        cur = conn.execute("delete from subscriptions where id=?", (sub_id,))
        conn.commit()
        if cur.rowcount == 0:
            return 404, {"code": 404, "msg": "订阅不存在"}
    except sqlite3.OperationalError as e:
        return 503, {"code": 503, "msg": "数据库忙，稍后重试: %s" % e}
    finally:
        conn.close()
    return 200, {"code": 0, "msg": "已删除"}


class Handler(BaseHTTPRequestHandler):
    server_version = "nasave-bypass/1.0"

    # ---------- helpers ----------
    def _cors(self):
        # 浏览器从官方 UI (:8888/:8889) 跨端口调用本服务，需放行跨源
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Bypass-Token")

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def authorized(self):
        if not API_TOKEN:
            return True
        return self.headers.get("X-Bypass-Token", "") == API_TOKEN

    def log_message(self, fmt, *args):
        # 安静模式：只记录非 2xx
        try:
            if int(args[1]) >= 400:
                super().log_message(fmt, *args)
        except (IndexError, ValueError):
            super().log_message(fmt, *args)

    # ---------- routes ----------
    def do_GET(self):
        if self.path == "/health":
            try:
                get_conn().execute("select 1").fetchone()
                return self.send_json(200, {"code": 0, "msg": "ok"})
            except Exception as e:
                return self.send_json(500, {"code": 500, "msg": str(e)})
        return self.send_json(404, {"code": 404, "msg": "not found"})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"code": 401, "msg": "unauthorized"})
        if self.path == "/bypass/subs":
            return self.send_json(*create_sub(self.read_body()))
        if re.fullmatch(r"/bypass/subs/\d+", self.path):
            return self.send_json(*delete_sub(int(self.path.rsplit("/", 1)[1])))
        return self.send_json(404, {"code": 404, "msg": "not found"})

    def do_OPTIONS(self):
        # CORS 预检
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()


def selftest():
    """内存库模拟全流程，不碰真实数据。"""
    import tempfile
    global DB_PATH
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "create table subscriptions(id integer primary key autoincrement,"
        " url text, name text, tpl_id int, interval int,"
        " last_sync text, status text, created_at text)")
    conn.commit()
    conn.close()
    DB_PATH = db

    code, r = create_sub({"url": "https://space.bilibili.com/x/favlist",
                          "name": "测试A", "tpl_id": 1, "interval": 60})
    assert (code, r["code"]) == (200, 0) and r["data"]["id"] == 1, (code, r)
    code, r = create_sub({"url": "https://www.xiaohongshu.com/u/xx",
                          "name": "测试B", "tpl_id": 2, "interval": 120})
    assert (code, r["code"]) == (200, 0) and r["data"]["id"] == 2, (code, r)
    code, r = create_sub({"url": "ftp://bad", "name": "坏", "tpl_id": 1})
    assert code == 400, (code, r)
    code, r = create_sub({"url": "https://ok.com/a", "name": "", "tpl_id": 1})
    assert code == 400, (code, r)
    code, r = create_sub({"url": "https://ok.com/a", "name": "x", "tpl_id": "abc"})
    assert code == 400, (code, r)
    code, r = delete_sub(1)
    assert (code, r["code"]) == (200, 0), (code, r)
    code, r = delete_sub(999)
    assert code == 404, (code, r)
    conn = sqlite3.connect(db)
    rows = conn.execute("select id,name from subscriptions").fetchall()
    conn.close()
    assert rows == [(2, "测试B")], rows
    print("SELFTEST OK: create x2, bad-url/empty-name/bad-tpl rejected, "
          "delete ok, delete-missing=404, remain=%r" % (rows,))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("bypass listening on 0.0.0.0:%d, db=%s, main=%s"
              % (LISTEN_PORT, DB_PATH, MAIN_APP))
        ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
