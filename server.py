"""
电诊 EV-Doctor 后端。纯 Python 标准库 + SQLite，不用装任何包：
    python server.py                 本机访问 http://127.0.0.1:8000
    python server.py 9000            换端口
    EVDOC_HOST=0.0.0.0 python server.py   允许局域网 / 公网访问（服务器部署用）

环境变量（都可选）：
    EVDOC_HOST          监听地址，默认 127.0.0.1
    EVDOC_PORT          端口，默认 8000（命令行参数优先）
    EVDOC_ADMIN_TOKEN   设了以后，非本机也能改"设置"，请求头带 X-Admin-Token: <值>
    EVDOC_LOG           日志文件路径，默认 data/server.log（同时打到终端）
    EVDOC_RATE          每个 IP 每分钟最多多少次诊断类请求，默认 120；0 表示不限
    EVDOC_BACKUP_HOUR   每天几点自动备份数据库（0-23），默认 3；-1 关闭

接口：
    GET  /healthz               健康检查（给 nginx / 监控用）
    GET  /api/info              数据库统计 + 大模型状态
    GET  /api/params            可录入的数据流参数
    GET  /api/vehicle           车辆平台选项（动力电池 / 低压电池 / 制动助力）
    GET  /api/faults            故障库列表（含案例数）
    GET  /api/faults/<id>       单个故障详情 + 维修案例
    GET  /api/cases?q=&fault_id=&limit=
    GET  /api/history?limit=
    POST /api/diagnose          {"text","measurements":{},"answers":{},"vehicle":{"battery":"lfp",...}}
    POST /api/confirm           {"diagnosis_id","fault_id","resolution","vehicle"}
  需要配置大模型（可选）：
    POST /api/explain           {"diagnosis_id"} → 流式文本
    POST /api/vision            {"image":"data:image/..."}
  设置（仅本机，或带管理口令）：
    GET  /api/settings
    POST /api/settings | /api/settings/test | /api/settings/models | /api/settings/clear-key
"""
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

import db
import llm
from engine import Engine

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
VERSION = "0.3"

# ---------------------------------------------------------------- 日志
log = logging.getLogger("evdoctor")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    path = os.environ.get("EVDOC_LOG", os.path.join(HERE, "data", "server.log"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        log.warning("日志文件打不开（%s），只输出到终端", e)


# ---------------------------------------------------------------- 全局状态
CONN = None
ENGINE = None
MAX_BODY = 8 * 1024 * 1024          # 照片最大 8MB
MAX_TEXT = 2000                     # 故障描述最长字数
ADMIN_TOKEN = os.environ.get("EVDOC_ADMIN_TOKEN", "").strip()
RATE = int(os.environ.get("EVDOC_RATE", "120"))

RECENT = OrderedDict()              # diagnosis_id -> 结果，供 /api/explain 用
RECENT_LOCK = threading.Lock()
_hits = {}                          # ip -> deque[timestamps]，简单限流
_hits_lock = threading.Lock()


def remember(result):
    with RECENT_LOCK:
        RECENT[result["diagnosis_id"]] = result
        while len(RECENT) > 500:
            RECENT.popitem(last=False)


def rate_limited(ip):
    if RATE <= 0:
        return False
    now = time.time()
    with _hits_lock:
        q = _hits.setdefault(ip, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE:
            return True
        q.append(now)
        if len(_hits) > 5000:  # 防止字典无限膨胀
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 120][:1000]:
                _hits.pop(k, None)
    return False


def llm_info():
    cfg = llm.load_config()
    ok = llm.llm_ready(cfg)
    return {"llm_ready": ok, "llm_model": cfg["model"] if ok else None,
            "vision_model": (cfg["vision_model"] or cfg["model"]) if ok else None}


# ---------------------------------------------------------------- 处理器
class Handler(BaseHTTPRequestHandler):
    server_version = f"EVDoctor/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ---- 工具
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _ip(self):
        # 经过 nginx 时真实 IP 在 X-Forwarded-For；直连时就是 client_address
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else self.client_address[0]) or "?"

    def _is_admin(self):
        if self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("X-Forwarded-For"):
            return True
        tok = self.headers.get("X-Admin-Token", "")
        return bool(ADMIN_TOKEN) and hmac.compare_digest(tok, ADMIN_TOKEN)

    def log_message(self, fmt, *args):
        pass  # 用自己的日志

    def _log(self, code, t0, note=""):
        log.info("%s %s %s %d %.0fms %s", self._ip(), self.command, self.path.split("?")[0], code,
                 (time.perf_counter() - t0) * 1000, note)

    # ---- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        t0 = time.perf_counter()
        u = urlparse(self.path)
        path, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            if path == "/healthz":
                db.counts(CONN)  # 顺便验证数据库可用
                return self._json({"ok": True, "version": VERSION, "time": db.now()})
            if path == "/api/info":
                return self._json({"ranker": ENGINE.ranker.name, "version": VERSION, **db.counts(CONN), **llm_info()})
            if path == "/api/settings":
                if not self._is_admin():
                    return self._err("只能在运行服务的这台电脑上查看设置", 403)
                return self._json(llm.public_config())
            if path == "/api/params":
                return self._json(db.get_params(CONN))
            if path == "/api/vehicle":
                keep = ("value", "label", "note", "hide_params")
                return self._json([{"id": d["id"], "name": d["name"],
                                    "options": [{k: o[k] for k in keep if k in o} for o in d["options"]]}
                                   for d in ENGINE.vehicle])
            if path == "/api/faults":
                keep = ("id", "name", "system", "severity", "hv_risk", "dtc", "keywords", "symptoms", "case_count")
                return self._json([{k: f[k] for k in keep} for f in db.get_faults(CONN)])
            if path.startswith("/api/faults/"):
                fid = path.rsplit("/", 1)[1]
                f = next((x for x in db.get_faults(CONN) if x["id"] == fid), None)
                if not f:
                    return self._err("没有这个故障类别", 404)
                f = dict(f)
                f.pop("examples", None)
                f["cases"] = db.get_cases(CONN, fault_id=fid, limit=50)
                return self._json(f)
            if path == "/api/cases":
                return self._json(db.get_cases(CONN, qs.get("q", "").strip()[:100], qs.get("fault_id", "")[:40],
                                               min(max(int(qs.get("limit", 100)), 1), 500)))
            if path == "/api/history":
                return self._json(db.get_history(CONN, min(max(int(qs.get("limit", 20)), 1), 200)))
            return self._err("not found", 404)
        except ValueError as e:
            return self._err(str(e))
        except Exception:  # noqa: BLE001  任何没想到的错误都不能让线程带着半个响应退出
            log.exception("GET %s 出错", path)
            return self._err("服务器内部错误，已记录日志", 500)
        finally:
            if path != "/healthz":
                self._log(getattr(self, "_code", 0), t0)

    def send_response(self, code, message=None):
        self._code = code
        super().send_response(code, message)

    # ---- POST
    def do_POST(self):
        t0 = time.perf_counter()
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True   # 请求体没读，这条连接不能复用
                return self._err("请求太大", 413)
            raw = self.rfile.read(n) if n else b""
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                return self._err("请求体应是 JSON 对象")
        except (ValueError, json.JSONDecodeError) as e:
            return self._err(f"请求格式错误：{e}")

        try:
            if path in ("/api/diagnose", "/api/explain", "/api/vision", "/api/confirm") and rate_limited(self._ip()):
                return self._err("请求太频繁，请稍后再试", 429)

            if path == "/api/diagnose":
                text = str(data.get("text") or "")[:MAX_TEXT]
                vehicle = data.get("vehicle") if isinstance(data.get("vehicle"), dict) else {}
                res = ENGINE.diagnose(text, data.get("measurements") or {}, data.get("answers") or {}, vehicle)
                remember(res)
                return self._json(res)

            if path == "/api/confirm":
                case_id = ENGINE.confirm(int(data.get("diagnosis_id") or 0), str(data.get("fault_id") or "")[:40],
                                         str(data.get("resolution") or "")[:500], str(data.get("vehicle") or "")[:100],
                                         (str(data["text"])[:MAX_TEXT] if data.get("text") else None))
                log.info("案例写回 #%d ← 诊断 #%s", case_id, data.get("diagnosis_id"))
                return self._json({"ok": True, "case_id": case_id, "case_count": db.counts(CONN)["case_count"]})

            if path == "/api/explain":
                return self._explain(data)

            if path == "/api/vision":
                return self._vision(data)

            if path in ("/api/settings", "/api/settings/test", "/api/settings/models", "/api/settings/clear-key"):
                if not self._is_admin():
                    return self._err("只能在运行服务的这台电脑上修改设置（或配置 EVDOC_ADMIN_TOKEN）", 403)
                if path == "/api/settings":
                    llm.save_config(data)
                    log.info("设置已更新：%s", llm.masked(llm.load_config()["key"]) or "无 key")
                    return self._json(llm.public_config())
                if path == "/api/settings/clear-key":
                    llm.clear_key()
                    return self._json(llm.public_config())
                try:
                    if path == "/api/settings/models":
                        return self._json({"models": llm.list_models(data)})
                    return self._json(llm.test_connection(data))
                except llm.LLMError as e:
                    return self._err(str(e), 502)

            return self._err("not found", 404)
        except ValueError as e:
            return self._err(str(e))
        except Exception:  # noqa: BLE001
            log.exception("POST %s 出错", path)
            return self._err("服务器内部错误，已记录日志", 500)
        finally:
            self._log(getattr(self, "_code", 0), t0)

    def _vision(self, data):
        img = data.get("image") or ""
        if not isinstance(img, str) or not img.startswith("data:image/"):
            return self._err("图片格式不对")
        try:
            out = llm.read_photo(img, db.get_params(CONN))
        except llm.LLMError as e:
            return self._err(f"照片识别失败：{e}", 502)
        params = {p["id"]: p for p in db.get_params(CONN)}
        clean = {}
        for k, v in (out.get("measurements") or {}).items():
            p = params.get(k)
            if not p:
                continue
            if p["type"] == "enum":
                if v in {o["value"] for o in p["options"]}:
                    clean[k] = v
            else:
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    pass
        out["measurements"] = clean
        return self._json(out)

    def _explain(self, data):
        with RECENT_LOCK:
            res = RECENT.get(int(data.get("diagnosis_id") or 0))
        if res is None:
            return self._err("找不到这次诊断，请重新诊断一次")
        try:
            gen = llm.stream_explain(res)
            first = next(gen, "")
        except llm.LLMError as e:
            return self._err(str(e), 502)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(first.encode("utf-8"))
            self.wfile.flush()
            for piece in gen:
                self.wfile.write(piece.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except llm.LLMError as e:
            try:
                self.wfile.write(f"\n\n> {e}".encode("utf-8"))
            except OSError:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


# ---------------------------------------------------------------- 后台任务
def backup_loop(hour):
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            log.info("自动备份 → %s", db.backup(CONN))
        except Exception:  # noqa: BLE001
            log.exception("自动备份失败")


def main():
    global CONN, ENGINE
    setup_logging()
    CONN = db.init_db()
    ENGINE = Engine(CONN)

    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("EVDOC_PORT", "8000"))
    host = os.environ.get("EVDOC_HOST", "127.0.0.1")
    srv = Server((host, port), Handler)

    c = db.counts(CONN)
    li = llm_info()
    log.info("电诊 EV-Doctor v%s 已启动：http://%s:%d", VERSION, "127.0.0.1" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    log.info("数据库 %s", db.DB_PATH)
    log.info("故障 %d 类 · 规则 %d 条 · 追问 %d 个 · 案例 %d 条", c["fault_count"], c["rule_count"], c["question_count"], c["case_count"])
    log.info("大模型（可选）：%s", "已接入 " + li["llm_model"] if li["llm_ready"] else "未配置，纯本地模式")
    if host not in ("127.0.0.1", "localhost") and not ADMIN_TOKEN:
        log.info("对外提供服务时，设置页只能在本机改；需要远程改请设 EVDOC_ADMIN_TOKEN")

    bh = int(os.environ.get("EVDOC_BACKUP_HOUR", "3"))
    if 0 <= bh <= 23:
        threading.Thread(target=backup_loop, args=(bh,), daemon=True, name="backup").start()

    def stop(*_):
        log.info("收到停止信号，正在关闭")
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        log.info("已停止")


if __name__ == "__main__":
    main()
