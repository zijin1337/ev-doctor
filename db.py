"""
数据库层：SQLite（Python 自带，不用装）。数据库文件是 data/evdoctor.db。

第一次启动时自动建表，并从 data/*.json 导入种子数据；之后所有读写都走数据库。
想恢复出厂数据：删掉 data/evdoctor.db 再启动即可（或 python db.py --reset）。

表结构：
  faults      故障知识库（19 类）
  fault_dtc   故障码 → 故障（一对多，便于按码查）
  params      数据流参数定义（技师可录入的测量项）
  rules       数据流阈值规则（国标 / 工程经验，带似然比和出处）
  questions   追问问题库（每个回答在各故障下的条件概率）
  cases       维修案例（种子案例 + 技师确认后写回的案例）
  diagnoses   每次诊断的记录（输入、初判、是否被确认）
"""
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta

from privacy import mask

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA, "evdoctor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS faults (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, system TEXT, severity TEXT, hv_risk INTEGER,
  symptoms TEXT, safety TEXT,
  keywords TEXT, examples TEXT, causes TEXT, steps TEXT, refs TEXT, dtc TEXT  -- JSON 数组
);
CREATE TABLE IF NOT EXISTS fault_dtc (
  code TEXT NOT NULL, fault_id TEXT NOT NULL REFERENCES faults(id), PRIMARY KEY (code, fault_id)
);
CREATE TABLE IF NOT EXISTS params (
  id TEXT PRIMARY KEY, name TEXT, unit TEXT, grp TEXT, type TEXT, options TEXT, hint TEXT, normal TEXT, ord INTEGER
);
CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY, param TEXT NOT NULL, op TEXT NOT NULL, value TEXT NOT NULL,
  lr TEXT NOT NULL, level TEXT, message TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, text TEXT NOT NULL, answers TEXT NOT NULL, p TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fault_id TEXT NOT NULL REFERENCES faults(id),
  vehicle TEXT, text TEXT NOT NULL, resolution TEXT,
  source TEXT NOT NULL DEFAULT 'confirmed',          -- seed 种子 / confirmed 技师确认
  diagnosis_id INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_fault ON cases(fault_id);
CREATE TABLE IF NOT EXISTS diagnoses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
  text TEXT, measurements TEXT, answers TEXT,
  top_fault TEXT, top_prob REAL, confirmed_fault TEXT
);
"""

_lock = threading.Lock()  # HTTP 服务是多线程的，写操作串行化


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _open(path):
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")      # 读写互不阻塞，多人同时用不会卡
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


class Database:
    """
    每个线程一条自己的 SQLite 连接。
    sqlite3 的连接不能让多个线程同时跑查询，HTTP 服务又是一请求一线程，
    所以用 threading.local 按线程分发连接，对外用起来和一条连接一样。
    """

    def __init__(self, path=DB_PATH):
        self.path = path
        self._local = threading.local()

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = _open(self.path)
        return c

    # 让旧代码里的 conn.execute(...) / with conn: 继续能用
    def execute(self, *a, **k):
        return self.conn.execute(*a, **k)

    def executescript(self, *a, **k):
        return self.conn.executescript(*a, **k)

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *a):
        return self.conn.__exit__(*a)

    def backup(self, dest_path):
        """在线备份（不用停服务）。"""
        dest = sqlite3.connect(dest_path)
        with dest:
            self.conn.backup(dest)
        dest.close()


def connect():
    return Database()


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def init_db(path=DB_PATH):
    """建表；如果是空库就导入种子数据。返回 Database。"""
    conn = Database(path)
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0] == 0:
        seed(conn)
    return conn


def backup(conn, keep=7):
    """备份到 data/backups/evdoctor-YYYYMMDD.db，只保留最近 keep 份。返回备份文件路径。"""
    bdir = os.path.join(os.path.dirname(conn.path), "backups")
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, f"evdoctor-{datetime.now():%Y%m%d}.db")
    with _lock:
        conn.backup(dest)
    olds = sorted(f for f in os.listdir(bdir) if f.startswith("evdoctor-") and f.endswith(".db"))
    for f in olds[:-keep]:
        try:
            os.remove(os.path.join(bdir, f))
        except OSError:
            pass
    return dest


def seed(conn):
    J = json.dumps
    refs = _load("refs.json")
    with conn:
        for f in _load("faults.json"):
            conn.execute(
                "INSERT INTO faults VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f["id"], f["name"], f["system"], f["severity"], int(f["hv_risk"]),
                 f["symptoms"], f["safety"],
                 J(f["keywords"], ensure_ascii=False), J(f["examples"], ensure_ascii=False),
                 J(f["causes"], ensure_ascii=False), J(f["steps"], ensure_ascii=False),
                 J(refs.get(f["id"], []), ensure_ascii=False), J(f["dtc"])))
            for code in f["dtc"]:
                conn.execute("INSERT OR IGNORE INTO fault_dtc VALUES (?,?)", (code.upper(), f["id"]))
        for i, p in enumerate(_load("params.json")):
            conn.execute("INSERT INTO params VALUES (?,?,?,?,?,?,?,?,?)",
                         (p["id"], p["name"], p.get("unit", ""), p["group"], p["type"],
                          J(p.get("options", []), ensure_ascii=False), p.get("hint", ""), p.get("normal", ""), i))
        for r in _load("rules.json"):
            conn.execute("INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
                         (r["id"], r["param"], r["op"], J(r["value"]), J(r["lr"]),
                          r["level"], r["message"], r.get("source")))
        for q in _load("questions.json"):
            conn.execute("INSERT INTO questions VALUES (?,?,?,?)",
                         (q["id"], q["text"], J(q["answers"], ensure_ascii=False), J(q["p"])))
        base = datetime.now()
        for c in _load("seed_cases.json"):
            ts = (base - timedelta(days=c.get("days_ago", 30), hours=c.get("days_ago", 30) % 9)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO cases (fault_id, vehicle, text, resolution, source, created_at) VALUES (?,?,?,?,?,?)",
                         (c["fault_id"], c.get("vehicle", ""), c["text"], c.get("resolution", ""), "seed", ts))


# ---------------- 读 ----------------

def _fault_row(r):
    d = dict(r)
    for k in ("keywords", "examples", "causes", "steps", "refs", "dtc"):
        d[k] = json.loads(d[k])
    d["hv_risk"] = bool(d["hv_risk"])
    return d


def get_faults(conn):
    rows = conn.execute("""SELECT f.*, (SELECT COUNT(*) FROM cases c WHERE c.fault_id = f.id) AS case_count
                           FROM faults f ORDER BY rowid""").fetchall()
    return [_fault_row(r) for r in rows]


def get_params(conn):
    out = []
    for r in conn.execute("SELECT * FROM params ORDER BY ord"):
        d = dict(r)
        d["group"] = d.pop("grp")
        d["options"] = json.loads(d["options"])
        d.pop("ord")
        out.append(d)
    return out


def get_rules(conn):
    out = []
    for r in conn.execute("SELECT * FROM rules ORDER BY rowid"):
        d = dict(r)
        d["value"] = json.loads(d["value"])
        d["lr"] = json.loads(d["lr"])
        out.append(d)
    return out


def get_questions(conn):
    return [{"id": r["id"], "text": r["text"], "answers": json.loads(r["answers"]), "p": json.loads(r["p"])}
            for r in conn.execute("SELECT * FROM questions ORDER BY rowid")]


CASE_SQL = """SELECT c.id, c.fault_id, f.name AS fault_name, c.vehicle, c.text, c.resolution, c.source, c.created_at
              FROM cases c JOIN faults f ON f.id = c.fault_id"""


def get_cases(conn, q="", fault_id="", limit=100):
    sql, args = CASE_SQL + " WHERE 1=1", []
    if fault_id:
        sql += " AND c.fault_id = ?"
        args.append(fault_id)
    if q:
        sql += " AND (c.text LIKE ? OR c.resolution LIKE ? OR c.vehicle LIKE ? OR f.name LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


def get_history(conn, limit=20):
    rows = conn.execute("""SELECT d.id, d.created_at, d.text, d.measurements, d.answers, d.top_fault, d.top_prob, d.confirmed_fault,
                                  f1.name AS top_name, f2.name AS confirmed_name
                           FROM diagnoses d
                           LEFT JOIN faults f1 ON f1.id = d.top_fault
                           LEFT JOIN faults f2 ON f2.id = d.confirmed_fault
                           ORDER BY d.id DESC LIMIT ?""", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["n_measurements"] = len(json.loads(d.pop("measurements") or "{}"))
        d["n_answers"] = len(json.loads(d.pop("answers") or "{}"))
        out.append(d)
    return out


def counts(conn):
    c = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return {"fault_count": c("faults"), "case_count": c("cases"), "rule_count": c("rules"),
            "question_count": c("questions"), "param_count": c("params"), "diagnosis_count": c("diagnoses")}


# ---------------- 写 ----------------

def save_diagnosis(conn, text, measurements, answers, top_fault, top_prob):
    """存一条诊断记录。描述先脱敏（手机号、车牌、VIN 等），原文不落盘。"""
    with _lock, conn:
        cur = conn.execute(
            "INSERT INTO diagnoses (created_at, text, measurements, answers, top_fault, top_prob) VALUES (?,?,?,?,?,?)",
            (now(), mask(text), json.dumps(measurements, ensure_ascii=False), json.dumps(answers), top_fault, top_prob))
        return cur.lastrowid


def confirm(conn, diagnosis_id, fault_id, resolution="", vehicle="", text=None):
    """技师确认最终故障：写回维修案例库，并标记诊断记录。返回新案例 id。描述、车辆、处理措施都先脱敏。"""
    with _lock, conn:
        if not conn.execute("SELECT 1 FROM faults WHERE id = ?", (fault_id,)).fetchone():
            raise ValueError("未知的故障类别")
        row = conn.execute("SELECT text, confirmed_fault FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()
        if row is None:
            raise ValueError("找不到这条诊断记录")
        if row["confirmed_fault"]:
            raise ValueError("这条诊断已经确认过了")
        desc = (text or row["text"] or "").strip()
        if not desc:
            raise ValueError("这次诊断没有文字描述，请补一句故障现象再存入")
        conn.execute("UPDATE diagnoses SET confirmed_fault = ? WHERE id = ?", (fault_id, diagnosis_id))
        cur = conn.execute(
            "INSERT INTO cases (fault_id, vehicle, text, resolution, source, diagnosis_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (fault_id, mask(vehicle.strip()), mask(desc), mask(resolution.strip()), "confirmed", diagnosis_id, now()))
        return cur.lastrowid


if __name__ == "__main__":
    if "--reset" in sys.argv and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("已删除旧数据库")
    c = init_db()
    print("数据库：", DB_PATH)
    for k, v in counts(c).items():
        print(f"  {k:16s} {v}")
