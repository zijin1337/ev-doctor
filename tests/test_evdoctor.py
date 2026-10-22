"""
单元测试。只用标准库 unittest，运行：
    python -m unittest discover -s tests -v
每个用例都用临时目录里的数据库，不会碰到 data/evdoctor.db。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db          # noqa: E402
import diagnoser   # noqa: E402
import engine      # noqa: E402
import llm         # noqa: E402
import privacy     # noqa: E402


class TmpDB(unittest.TestCase):
    """基类：每个测试一个干净的临时数据库。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evdoc-")
        self.conn = db.init_db(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- 数据文件
class TestDataFiles(unittest.TestCase):
    def setUp(self):
        d = os.path.join(ROOT, "data")
        load = lambda n: json.load(open(os.path.join(d, n), encoding="utf-8"))
        self.faults, self.rules, self.params = load("faults.json"), load("rules.json"), load("params.json")
        self.questions, self.cases, self.refs = load("questions.json"), load("seed_cases.json"), load("refs.json")
        self.ids = {f["id"] for f in self.faults}

    def test_faults_fields(self):
        need = {"id", "name", "system", "severity", "hv_risk", "dtc", "keywords", "symptoms", "examples", "causes", "steps", "safety"}
        for f in self.faults:
            self.assertTrue(need <= set(f), f["id"])
            self.assertIn(f["severity"], ("high", "medium", "low"))
            self.assertGreaterEqual(len(f["keywords"]), 8, f["id"])

    def test_rules_reference_known_params_and_faults(self):
        pids = {p["id"] for p in self.params} | set(engine.DERIVED)
        for r in self.rules:
            self.assertIn(r["param"], pids, r["id"])
            self.assertTrue(set(r["lr"]) <= self.ids, r["id"])
            self.assertIn(r["level"], ("fault", "warn", "ok"))
            for v in r["lr"].values():
                self.assertGreater(v, 0)

    def test_rule_ranges_do_not_overlap(self):
        """同一参数的数值区间不能互相重叠，否则一个读数会同时触发两条矛盾规则。"""
        by_param = {}
        for r in self.rules:
            if r["op"] != "==":
                by_param.setdefault(r["param"], []).append(r)
        for p, rs in by_param.items():
            for x in (-1e9, -50, -10, 0, 1, 5, 10, 11.9, 12.3, 13.2, 14, 15, 40, 50, 60, 80, 100, 120, 200, 400, 1000, 1e9):
                hits = [r["id"] for r in rs if engine._Session._match(r, x)]
                self.assertLessEqual(len(hits), 1, f"{p}={x} 命中多条: {hits}")

    def test_questions_probabilities(self):
        for q in self.questions:
            keys = [a["key"] for a in q["answers"]]
            self.assertEqual(set(q["p"]), self.ids, q["id"])
            for fid, dist in q["p"].items():
                self.assertEqual(set(dist), set(keys), f"{q['id']}/{fid}")
                self.assertAlmostEqual(sum(dist.values()), 1.0, places=6)
                for v in dist.values():
                    self.assertTrue(0.03 <= v <= 0.97, f"{q['id']}/{fid}: {v}")

    def test_cases_and_refs(self):
        for c in self.cases:
            self.assertIn(c["fault_id"], self.ids)
        self.assertEqual(set(self.refs), self.ids)


# ---------------------------------------------------------------- 文本模型
class TestDiagnoser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = diagnoser.get_ranker()

    def test_probabilities_sum_to_one(self):
        p = dict(self.r.rank("充不进电"))
        self.assertAlmostEqual(sum(p.values()), 1.0, places=6)

    def test_dtc_match_without_word_boundary(self):
        """中文和故障码之间没有空格也要能识别。"""
        top = self.r.rank("扫码出来P0A0A，换完PTC暖风之后出现的")[0][0]
        self.assertEqual(top, "HVIL_OPEN")
        self.assertEqual(self.r.rank("p0aa6")[0][0], "INSULATION_LOW")

    def test_never_fully_certain(self):
        self.assertLess(self.r.rank("P0A0A")[0][1], 0.99)

    def test_obvious_cases(self):
        for text, fid in [("仪表报绝缘故障", "INSULATION_LOW"), ("电机过热请减速", "MOTOR_OVERTEMP"),
                          ("遥控钥匙没反应仪表全黑", "LV_BATTERY_LOW"), ("刹车踏板变硬", "BRAKE_VACUUM")]:
            self.assertEqual(self.r.rank(text)[0][0], fid, text)


# ---------------------------------------------------------------- 数据库
class TestDB(TmpDB):
    def test_seeded(self):
        c = db.counts(self.conn)
        self.assertEqual(c["fault_count"], 19)
        self.assertEqual(c["case_count"], 38)
        self.assertGreater(c["rule_count"], 40)

    def test_confirm_writes_case_and_marks_diagnosis(self):
        did = db.save_diagnosis(self.conn, "测试描述", {}, {}, "HVIL_OPEN", 0.5)
        cid = db.confirm(self.conn, did, "INSULATION_LOW", "换密封条", "测试车")
        self.assertEqual(db.counts(self.conn)["case_count"], 39)
        h = db.get_history(self.conn, 1)[0]
        self.assertEqual(h["confirmed_fault"], "INSULATION_LOW")
        c = db.get_cases(self.conn, fault_id="INSULATION_LOW")[0]
        self.assertEqual((c["id"], c["source"], c["text"]), (cid, "confirmed", "测试描述"))

    def test_confirm_rejects_bad_input(self):
        did = db.save_diagnosis(self.conn, "", {}, {}, "HVIL_OPEN", 0.5)
        with self.assertRaises(ValueError):
            db.confirm(self.conn, did, "NOPE")
        with self.assertRaises(ValueError):
            db.confirm(self.conn, did, "HVIL_OPEN")  # 没有描述
        db.confirm(self.conn, did, "HVIL_OPEN", text="补的描述")
        with self.assertRaises(ValueError):
            db.confirm(self.conn, did, "HVIL_OPEN", text="再确认一次")  # 重复确认

    def test_search(self):
        self.assertTrue(db.get_cases(self.conn, q="绝缘"))
        self.assertEqual(db.get_cases(self.conn, q="不存在的词xyz"), [])

    def test_threads_each_get_own_connection(self):
        """每个线程拿到的是不同的连接对象（同时存活时互不相同）。"""
        conns, gate = [], threading.Barrier(4)
        def f():
            c = self.conn.conn
            gate.wait()            # 四个线程都拿到连接后再记录，避免对象被回收后 id 复用
            conns.append(c)
            gate.wait()
        ts = [threading.Thread(target=f) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len({id(c) for c in conns}), 4)
        main = self.conn.conn
        self.assertTrue(all(c is not main for c in conns))

    def test_backup(self):
        path = db.backup(self.conn)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(db.init_db(path).execute("SELECT COUNT(*) FROM faults").fetchone()[0], 19)


# ---------------------------------------------------------------- 诊断引擎
class TestEngine(TmpDB):
    def setUp(self):
        super().setUp()
        self.e = engine.Engine(self.conn)

    def test_garbage_text_gives_no_report(self):
        """"123" 这类描述不能靠字符巧合猜出一个故障来。"""
        for t in ("123", "abc", "？？？", "今天天气不错"):
            r = self.e.diagnose(text=t)
            self.assertFalse(r["text_used"], t)
            self.assertEqual(r["status"], "need_more", t)
            self.assertIsNone(r["report"], t)
            self.assertIsNotNone(r["next_question"], t)
            self.assertLess(r["candidates"][0]["prob"], 0.1, t)   # 均匀先验，没有被某一类抢走
        # 模糊但有中文的描述：也先追问
        self.assertEqual(self.e.diagnose(text="车坏了")["status"], "need_more")
        # 有效描述：正常出报告
        r = self.e.diagnose(text="充不进电")
        self.assertTrue(r["text_used"] and r["status"] == "ok" and r["report"])
        # 垃圾描述 + 数据流：数据流是硬证据，可以出报告
        r = self.e.diagnose(text="123", measurements={"lv_voltage_off": 12.1, "lv_voltage_ready": 12.3})
        self.assertEqual((r["status"], r["candidates"][0]["id"]), ("ok", "DCDC_FAULT"))

    def test_requires_some_input(self):
        with self.assertRaises(ValueError):
            self.e.diagnose()

    def test_measurement_validation(self):
        with self.assertRaises(ValueError):
            self.e.diagnose(measurements={"pack_voltage": "abc"})
        r = self.e.diagnose(measurements={"unknown_param": 1, "hvil_state": "bogus", "pack_voltage": "380"})
        self.assertEqual(r["derived"], {})  # 非法的全被丢掉，只剩 pack_voltage，算不出派生量

    def test_rules_change_result_with_source(self):
        text = "车上不了高压，仪表报动力系统故障"
        before = dict((c["id"], c["prob"]) for c in self.e.diagnose(text=text)["candidates"])
        r = self.e.diagnose(text=text, measurements={"pack_voltage": 380, "insulation_kohm": 25, "hvil_state": "closed"})
        self.assertEqual(r["candidates"][0]["id"], "INSULATION_LOW")
        self.assertGreater(r["candidates"][0]["prob"], before.get("INSULATION_LOW", 0))
        self.assertAlmostEqual(r["derived"]["insul_ohm_per_v"]["value"], 65.79, places=1)
        fault_ev = [e for e in r["evidence"] if e["level"] == "fault"]
        self.assertTrue(fault_ev and "GB 18384" in fault_ev[0]["source"])
        self.assertTrue(any(e["effect"] == "refute" for e in r["evidence"]))  # 互锁闭合 → 排除

    def test_question_flow_converges(self):
        ans, r = {}, None
        for _ in range(8):
            r = self.e.diagnose(text="车上不了高压", answers=ans)
            q = r["next_question"]
            if not q:
                break
            self.assertNotIn(q["id"], ans)             # 不重复问
            self.assertGreater(q["gain"], 0)
            p = self.e.q_by_id[q["id"]]["p"]["PRECHARGE_FAIL"]
            ans[q["id"]] = max(p, key=p.get)           # 按预充失败来答
        self.assertEqual(r["candidates"][0]["id"], "PRECHARGE_FAIL")
        self.assertEqual(len(r["answered"]), len(ans))

    def test_unknown_answer_is_neutral(self):
        a = self.e.diagnose(text="充不进电")["candidates"][0]["prob"]
        b = self.e.diagnose(text="充不进电", answers={"Q_WATER": "unknown"})["candidates"][0]["prob"]
        self.assertAlmostEqual(a, b, places=6)

    def test_question_skipped_when_measured(self):
        r = self.e.diagnose(measurements={"lv_voltage_off": 12.1, "lv_voltage_ready": 12.3})
        self.assertNotEqual((r["next_question"] or {}).get("id"), "Q_LV_VOLT_READY")

    def test_confirm_makes_future_matches_better(self):
        odd = "仪表盘右下角那个小图标一闪一闪然后车就动不了了"
        r1 = self.e.diagnose(text=odd)
        self.e.confirm(r1["diagnosis_id"], "RELAY_WELD", "更换主正继电器")
        r2 = self.e.diagnose(text=odd)
        p1 = next(c["prob"] for c in r1["candidates"] if c["id"] == "RELAY_WELD") if any(c["id"] == "RELAY_WELD" for c in r1["candidates"]) else 0
        self.assertEqual(r2["candidates"][0]["id"], "RELAY_WELD")
        self.assertGreater(r2["candidates"][0]["prob"], p1)
        self.assertEqual(r2["similar_cases"][0]["source"], "confirmed")

    def test_concurrent_diagnose_during_rebuild(self):
        errs = []
        def w():
            try:
                for _ in range(20):
                    self.e.diagnose(text="上不了高压")
            except Exception as ex:  # noqa: BLE001
                errs.append(ex)
        def c():
            for i in range(5):
                r = self.e.diagnose(text=f"并发写回 {i}")
                self.e.confirm(r["diagnosis_id"], "HVIL_OPEN")
        ts = [threading.Thread(target=w) for _ in range(8)] + [threading.Thread(target=c)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])


# ---------------------------------------------------------------- 数据脱敏
class TestPrivacy(TmpDB):
    def test_mask_patterns(self):
        m = privacy.mask
        self.assertEqual(m("车主电话13812345678，回电"), "车主电话138****5678，回电")
        self.assertEqual(m("粤B12345 上不了高压"), "粤B***45 上不了高压")
        self.assertEqual(m("新能源牌照粤BD12345"), "新能源牌照粤B****45")
        self.assertEqual(m("VIN LSVAU2180N2183294 已核对"), "VIN LSV**********3294 已核对")
        self.assertEqual(m("身份证110101199003071234"), "身份证110101********1234")

    def test_diagnostic_text_untouched(self):
        for s in ("扫码出来P0A0A，换完PTC之后出现的", "U0111 BMS 通讯丢失", "绝缘电阻 25 kΩ，电池 380 V",
                  "CAN 电阻 60 Ω，CC1 4 V", "", None):
            self.assertEqual(privacy.mask(s), s)

    def test_db_stores_masked(self):
        did = db.save_diagnosis(self.conn, "粤B12345 车主13812345678 上不了高压", {}, {}, "INSULATION_LOW", 0.5)
        row = self.conn.execute("SELECT text FROM diagnoses WHERE id = ?", (did,)).fetchone()
        self.assertEqual(row["text"], "粤B***45 车主138****5678 上不了高压")
        cid = db.confirm(self.conn, did, "INSULATION_LOW", "联系车主 13900001111 取车", "粤BD12345 LSVAU2180N2183294")
        case = self.conn.execute("SELECT text, vehicle, resolution FROM cases WHERE id = ?", (cid,)).fetchone()
        self.assertNotIn("13812345678", case["text"])
        self.assertEqual(case["vehicle"], "粤B****45 LSV**********3294")
        self.assertEqual(case["resolution"], "联系车主 139****1111 取车")

    def test_explain_prompt_masked(self):
        res = {"text": "粤B12345 上不了高压", "candidates": [], "evidence": [], "answered": [],
               "report": {"name": "x", "causes": [], "steps": [], "safety": "", "refs": []}}
        self.assertIn("粤B***45", llm.build_explain_prompt(res))
        self.assertNotIn("粤B12345", llm.build_explain_prompt(res))


# ---------------------------------------------------------------- 大模型层（用本地假接口）
class _Fake(BaseHTTPRequestHandler):
    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer sk-ok":
            return self._j(401, {"error": {"message": "Incorrect API key"}})
        self._j(200, {"data": [{"id": "chat-a"}, {"id": "vl-b"}, {"id": "text-embedding-c"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer sk-ok":
            return self._j(401, {"error": {"message": "Incorrect API key"}})
        if body["model"] not in ("chat-a", "vl-b"):
            return self._j(400, {"error": {"message": "Model Not Exist"}})
        img = any(isinstance(m.get("content"), list) for m in body["messages"])
        if img and body["model"] != "vl-b":
            return self._j(400, {"error": {"message": "does not support image"}})
        if img and "JSON" in body["messages"][0]["content"][0]["text"]:
            content = '```json\n{"description":"绝缘灯亮","dtc":["P0AA6","XYZ"],"measurements":{"insulation_kohm":"30","nope":1}}\n```'
        else:
            content = "红色" if img else "好"
        self._j(200, {"choices": [{"message": {"content": content}}]})

    def log_message(self, *a):
        pass


class TestLLM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Fake)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls._env = {k: os.environ.pop(k, None) for k in ("EVDOC_API_BASE", "EVDOC_API_KEY", "EVDOC_MODEL", "EVDOC_VISION_MODEL")}
        cls._cfg_path, llm.CONFIG_PATH = llm.CONFIG_PATH, os.path.join(tempfile.mkdtemp(), "config.json")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        llm.CONFIG_PATH = cls._cfg_path
        for k, v in cls._env.items():
            if v is not None:
                os.environ[k] = v

    def test_not_configured(self):
        self.assertFalse(llm.llm_ready())
        with self.assertRaises(llm.LLMError):
            list(llm.stream_explain({}))

    def test_base_normalisation(self):
        self.assertEqual(llm._norm_base("api.x.com/v1/chat/completions/"), "https://api.x.com/v1")
        self.assertEqual(llm._with_v1("https://a.com/v4"), None)
        self.assertEqual(llm._with_v1("https://a.com"), "https://a.com/v1")

    def test_list_models_and_errors(self):
        self.assertEqual(llm.list_models({"api_base": self.base, "api_key": "sk-ok"}), ["chat-a", "text-embedding-c", "vl-b"])
        with self.assertRaisesRegex(llm.LLMError, "401"):
            llm.list_models({"api_base": self.base, "api_key": "sk-bad"})
        # 地址漏了 /v1 也能自动补
        self.assertTrue(llm.list_models({"api_base": self.base[:-3], "api_key": "sk-ok"}))

    def test_test_connection(self):
        r = llm.test_connection({"api_base": self.base, "api_key": "sk-ok", "model": "chat-a", "vision_model": "vl-b"})
        self.assertTrue(r["text"]["ok"] and r["vision"]["ok"])
        r = llm.test_connection({"api_base": self.base, "api_key": "sk-ok", "model": "chat-a", "vision_model": ""})
        self.assertTrue(r["text"]["ok"])
        self.assertFalse(r["vision"]["ok"])
        self.assertIn("不能看图", r["vision"]["error"])
        r = llm.test_connection({"api_base": self.base, "api_key": "sk-ok", "model": "typo"})
        self.assertIn("模型名不存在", r["text"]["error"])

    def test_save_keeps_key_when_blank(self):
        llm.save_config({"api_base": self.base, "api_key": "sk-ok", "model": "chat-a"})
        llm.save_config({"api_base": self.base, "api_key": "", "model": "chat-a", "vision_model": "vl-b"})
        cfg = llm.load_config()
        self.assertEqual((cfg["key"], cfg["vision_model"]), ("sk-ok", "vl-b"))
        self.assertNotIn("sk-ok", json.dumps(llm.public_config()))
        llm.clear_key()
        self.assertFalse(llm.llm_ready())

    def test_read_photo_filters(self):
        llm.save_config({"api_base": self.base, "api_key": "sk-ok", "model": "chat-a", "vision_model": "vl-b"})
        params = [{"id": "insulation_kohm", "name": "绝缘", "unit": "kΩ", "type": "number", "options": []}]
        out = llm.read_photo("data:image/png;base64,AAAA", params)
        self.assertEqual(out["dtc"], ["P0AA6"])           # 假码 XYZ 被过滤
        self.assertEqual(out["description"], "绝缘灯亮")
        llm.clear_key()


if __name__ == "__main__":
    unittest.main(verbosity=2)
