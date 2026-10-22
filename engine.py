"""
诊断引擎：把几路证据用贝叶斯公式融合成一个概率分布。不调用任何大模型。

    P(故障 | 所有证据) ∝ P(故障 | 文字描述) × Π 规则似然比 × Π P(回答 | 故障)

  1. 文字描述 → diagnoser.TfidfRanker（以后换成小 Jev），没写描述就用均匀先验
  2. 数据流  → rules 表里的国标 / 工程阈值，每条命中的规则给相关故障乘一个似然比
  3. 追问    → questions 表里的条件概率；下一个问题挑"期望信息增益"最大的
  4. 相似案例 → 在 cases 表里按字符 n-gram 余弦找最像的历史维修记录
  5. 车辆平台 → data/vehicle_profiles.json：按动力电池、低压电池、制动方式改阈值、停用不适用的规则

为了让"越用越准"真的生效，技师确认后的案例会并进 TF-IDF 的语料（rebuild()）。
"""
import json
import math
import os
import threading
import time

import db
from diagnoser import DTC_RE, TfidfRanker, char_ngrams

# 文本模型的概率取这个次方再归一化。TF-IDF 对短句会"过度自信"（"上不了高压"就给 97%），
# 压平之后 Top-1 不变，但模糊描述的置信度会降下来，让追问和数据流有机会发挥作用
TEXT_TEMPER = 0.6

# 文字描述至少要"像一句故障描述"才拿来算先验：
#   有中文，或有故障码，或命中了知识库关键词（BMS / DCDC / READY 这类英文缩写也算）；
#   并且和知识库的最高匹配分不能太低。否则当作"没说清楚"，用均匀先验直接进入追问。
MIN_TEXT_SIGNAL = 0.15
# 没有任何依据、首位概率又低于这个值时，不出诊断报告，只追问
MIN_REPORT_PROB = 0.25

# 已经录入了对应数据流，就不再问同一件事
QUESTION_COVERED_BY = {
    "Q_LV_VOLT_READY": ["lv_voltage_ready"],
    "Q_COOLANT": ["coolant_level"],
}

VEHICLE_PATH = os.path.join(db.DATA, "vehicle_profiles.json")


def load_vehicle_profiles(path=VEHICLE_PATH):
    """车辆平台档案：每个维度（动力电池 / 低压电池 / 制动助力）的选项，以及选中后怎么调整规则。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


DERIVED = {
    "insul_ohm_per_v": ("绝缘电阻（换算）", "Ω/V"),
    "cell_diff_mv": ("单体压差", "mV"),
    "temp_diff": ("电池包温差", "℃"),
    "precharge_ratio": ("预充比例", "%"),
}


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.1f}" if abs(v) < 1000 else f"{v:.0f}"
    return str(v)


class _Knowledge:
    """一次 rebuild 的产物，整体只读。换新版本时整个对象替换，正在诊断的请求继续用旧的，不会读到半成品。"""

    def __init__(self, conn):
        self.faults = db.get_faults(conn)
        self.by_id = {f["id"]: f for f in self.faults}
        self.ids = [f["id"] for f in self.faults]
        self.params = {p["id"]: p for p in db.get_params(conn)}
        self.rules = db.get_rules(conn)
        self.questions = db.get_questions(conn)
        self.q_by_id = {q["id"]: q for q in self.questions}
        self.cases = db.get_cases(conn, limit=100000)
        self.vehicle = load_vehicle_profiles()
        self.vehicle_index = {(d["id"], o["value"]): (d, o) for d in self.vehicle for o in d["options"] if o["value"]}

        # 案例描述加进对应故障的 examples，确认得越多，这类说法就认得越准
        aug = []
        for f in self.faults:
            g = dict(f)
            g["examples"] = f["examples"] + [c["text"] for c in self.cases if c["fault_id"] == f["id"]]
            aug.append(g)
        self.ranker = TfidfRanker(aug)

        # 相似案例检索用的向量
        self.case_vecs = [(c, self.ranker._vec(c["text"] + " " + (c["resolution"] or ""))) for c in self.cases]


class Engine:
    def __init__(self, conn):
        self.conn = conn
        self._lock = threading.Lock()
        self._k = _Knowledge(conn)

    # ------------------------------------------------------------------ 载入
    def rebuild(self):
        """从数据库重新载入知识库。构建在锁外完成，最后原子替换。"""
        k = _Knowledge(self.conn)
        with self._lock:
            self._k = k

    # 旧代码和测试里用的属性，都转发到当前知识库快照
    faults = property(lambda s: s._k.faults)
    by_id = property(lambda s: s._k.by_id)
    ids = property(lambda s: s._k.ids)
    params = property(lambda s: s._k.params)
    rules = property(lambda s: s._k.rules)
    questions = property(lambda s: s._k.questions)
    q_by_id = property(lambda s: s._k.q_by_id)
    cases = property(lambda s: s._k.cases)
    ranker = property(lambda s: s._k.ranker)
    case_vecs = property(lambda s: s._k.case_vecs)
    vehicle = property(lambda s: s._k.vehicle)

    def diagnose(self, text="", measurements=None, answers=None, vehicle=None):
        # 固定本次请求用的知识库快照；期间别的线程 rebuild 也不影响这里
        return _Session(self, self._k).diagnose(text, measurements, answers, vehicle)

    def confirm(self, diagnosis_id, fault_id, resolution="", vehicle="", text=None):
        case_id = db.confirm(self.conn, diagnosis_id, fault_id, resolution, vehicle, text)
        self.rebuild()  # 新案例立刻生效
        return case_id


class _Session:
    """一次诊断。所有 self.xxx 读的都是固定的快照 k，而不是 Engine 上可能被替换的属性。"""

    def __init__(self, engine, k):
        self.conn = engine.conn
        self.faults, self.by_id, self.ids = k.faults, k.by_id, k.ids
        self.params, self.rules = k.params, k.rules
        self.questions, self.q_by_id = k.questions, k.q_by_id
        self.cases, self.ranker, self.case_vecs = k.cases, k.ranker, k.case_vecs
        self.vehicle_index = k.vehicle_index

    # ------------------------------------------------------------------ 车辆平台
    def _apply_vehicle(self, vehicle):
        """按车辆平台调整本次诊断用的规则：改阈值、停用不适用的规则、调整个别故障的先验。
        只改这次会话的副本，不影响知识库。返回 (认出的选择, 说明, 故障先验倍数)。"""
        chosen, notes, mult, overrides, disabled = {}, [], {}, {}, set()
        self.report_notes = {}  # 故障 id → 报告里要补充的说明（比如锂电低压电池的电压标准不同）
        for dim, value in (vehicle if isinstance(vehicle, dict) else {}).items():
            hit = self.vehicle_index.get((dim, value)) if isinstance(dim, str) and isinstance(value, str) else None
            if hit is None:
                continue  # 不认识的维度或取值直接忽略，按通用阈值算
            d, o = hit
            chosen[dim] = value
            overrides.update(o.get("rules", {}))
            disabled.update(o.get("disable", []))
            for fid, x in o.get("faults", {}).items():
                mult[fid] = mult.get(fid, 1.0) * x
            for fid, text in o.get("report_notes", {}).items():
                self.report_notes.setdefault(fid, []).append(text)
            notes.append({"dim": d["name"], "label": o["label"], "note": o.get("note", "")})
        if overrides or disabled:
            self.rules = [dict(r, **overrides[r["id"]]) if r["id"] in overrides else r
                          for r in self.rules if r["id"] not in disabled]
        return chosen, notes, mult

    # ------------------------------------------------------------------ 数据流
    def _text_has_signal(self, text, items):
        """描述里有没有能用来判断的内容。"""
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)
        has_dtc = bool(DTC_RE.search(text))
        has_kw = any(it["keywords"] for it in items)
        if not (has_cjk or has_dtc or has_kw):
            return False            # 纯数字 / 字母 / 符号，比如 "123" "abc" "???"
        if has_dtc or has_kw:
            return True
        top = max(self.ranker.scores(text).values(), key=lambda v: v["score"])["score"]
        return top >= MIN_TEXT_SIGNAL

    def _measurements(self, raw):
        """清洗前端传来的测量值，并计算派生量。"""
        m = {}
        for k, v in (raw or {}).items():
            p = self.params.get(k)
            if p is None or v in (None, ""):
                continue
            if p["type"] == "enum":
                if v in {o["value"] for o in p["options"]}:
                    m[k] = v
            else:
                try:
                    m[k] = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{p['name']} 应该填数字")
        if "insulation_kohm" in m and m.get("pack_voltage", 0) > 0:
            m["insul_ohm_per_v"] = m["insulation_kohm"] * 1000 / m["pack_voltage"]
        if "cell_max_v" in m and "cell_min_v" in m:
            m["cell_diff_mv"] = (m["cell_max_v"] - m["cell_min_v"]) * 1000
        if "cell_max_t" in m and "cell_min_t" in m:
            m["temp_diff"] = m["cell_max_t"] - m["cell_min_t"]
        if "precharge_bus_v" in m and m.get("pack_voltage", 0) > 0:
            m["precharge_ratio"] = m["precharge_bus_v"] / m["pack_voltage"] * 100
        return m

    @staticmethod
    def _match(rule, x):
        op, v = rule["op"], rule["value"]
        if op == "==":
            return x == v
        if isinstance(x, str):
            return False
        if op == "<":
            return x < v
        if op == "<=":
            return x <= v
        if op == ">":
            return x > v
        if op == ">=":
            return x >= v
        if op == "between":          # 左闭右开 [lo, hi)，相邻区间 [a,b) [b,c) 不会在边界上同时命中
            return v[0] <= x < v[1]
        if op == "outside":
            return x < v[0] or x > v[1]
        return False

    def _apply_rules(self, m):
        hits = []
        for r in self.rules:
            if r["param"] in m and self._match(r, m[r["param"]]):
                hits.append((r, m[r["param"]]))
        return hits

    # ------------------------------------------------------------------ 概率
    def _posterior(self, prior, rule_hits, answers, mult=None):
        logp = {fid: math.log(max(prior.get(fid, 0), 1e-9)) for fid in self.ids}
        for fid, x in (mult or {}).items():  # 车辆平台带来的先验调整
            if fid in logp:
                logp[fid] += math.log(x)
        for r, _ in rule_hits:
            for fid, lr in r["lr"].items():
                if fid in logp:
                    logp[fid] += math.log(lr)
        for qid, key in answers.items():
            q = self.q_by_id.get(qid)
            if q and key != "unknown":
                for fid in self.ids:
                    logp[fid] += math.log(max(q["p"].get(fid, {}).get(key, 0.5), 1e-6))
        mx = max(logp.values())
        e = {fid: math.exp(v - mx) for fid, v in logp.items()}
        z = sum(e.values())
        return {fid: v / z for fid, v in e.items()}

    @staticmethod
    def _entropy(dist):
        return -sum(p * math.log2(p) for p in dist.values() if p > 0)

    def _next_question(self, post, answers, m=None):
        """挑期望信息增益最大的问题：问完以后，候选的不确定性平均能降多少。"""
        h0 = self._entropy(post)
        best, best_gain = None, 0.0
        for q in self.questions:
            if q["id"] in answers or any(k in (m or {}) for k in QUESTION_COVERED_BY.get(q["id"], [])):
                continue
            exp_h = 0.0
            for a in q["answers"]:
                k = a["key"]
                pa = sum(post[f] * q["p"][f][k] for f in self.ids)
                if pa <= 0:
                    continue
                cond = {f: post[f] * q["p"][f][k] / pa for f in self.ids}
                exp_h += pa * self._entropy(cond)
            gain = h0 - exp_h
            if gain > best_gain:
                best, best_gain = q, gain
        # 已经很确定，或者剩下的问题帮助不大，就不再追问
        if best is None or best_gain < 0.08 or max(post.values()) > 0.9:
            return None
        return {"id": best["id"], "text": best["text"], "answers": best["answers"], "gain": round(best_gain, 3)}

    def _suggest_measure(self, ranked, m, k=3):
        """问答分不开时，告诉技师下一步测哪个量：挑对前几名候选区分力最大、还没测的参数。"""
        top = [fid for fid, p in ranked[:3] if p > 0.05]
        if len(top) < 2 and ranked[0][1] > 0.75:
            return []
        score = {}
        for r in self.rules:
            base = r["param"]
            # 派生量映射回需要录入的原始参数
            raw = {"insul_ohm_per_v": "insulation_kohm", "cell_diff_mv": "cell_min_v",
                   "temp_diff": "cell_min_t", "precharge_ratio": "precharge_bus_v"}.get(base, base)
            if raw in m or raw not in self.params:
                continue
            for fid in top:
                lr = r["lr"].get(fid)
                if lr:
                    score.setdefault(raw, {}).setdefault(fid, 0)
                    score[raw][fid] = max(score[raw][fid], abs(math.log(lr)))
        best = sorted(score.items(), key=lambda kv: -sum(kv[1].values()))[:k]
        out = []
        for pid, fs in best:
            p = self.params[pid]
            fid = max(fs, key=fs.get)
            out.append({"id": pid, "name": p["name"], "unit": p["unit"], "hint": p["hint"],
                        "normal": p["normal"], "for": self.by_id[fid]["name"]})
        return out

    # ------------------------------------------------------------------ 相似案例
    def similar_cases(self, text, top_fault=None, k=3):
        if not text:
            # 没有描述时，按最可能的故障列最近的案例
            out = [dict(c, sim=None) for c in self.cases if c["fault_id"] == top_fault][:k]
            return out
        q = self.ranker._vec(text)
        scored = []
        for c, v in self.case_vecs:
            s = self.ranker._cos(q, v)
            if c["fault_id"] == top_fault:
                s *= 1.15  # 同类故障的案例稍微靠前
            scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [dict(c, sim=round(min(s, 1.0), 3)) for s, c in scored[:k] if s > 0.05]

    # ------------------------------------------------------------------ 主入口
    def diagnose(self, text="", measurements=None, answers=None, vehicle=None):
        t0 = time.perf_counter()
        text = (text or "").strip()
        answers = {k: v for k, v in (answers or {}).items() if k in self.q_by_id}
        chosen, vnotes, vmult = self._apply_vehicle(vehicle)
        m = self._measurements(measurements)
        if not text and not m and not answers:
            raise ValueError("请写一句故障描述，或者至少录入一项数据流")

        evidence = []
        text_used = False
        # 1) 文字描述
        items = self.ranker.explain(text) if text else []
        if text and self._text_has_signal(text, items):
            text_used = True
            prior = {it["id"]: it["prob"] ** TEXT_TEMPER for it in items}
            z = sum(prior.values())
            prior = {k: v / z for k, v in prior.items()}
            for code in sorted({c.upper() for c in DTC_RE.findall(text)}):
                hit = [it["id"] for it in items if code in it["dtc"]]
                if hit:
                    evidence.append({"kind": "dtc", "level": "fault", "effect": "support",
                                     "text": f"故障码 {code} 对应「{self.by_id[hit[0]]['name']}」", "source": "SAE J2012 通用故障码"})
                else:
                    evidence.append({"kind": "dtc", "level": "info", "effect": "support",
                                     "text": f"故障码 {code} 不在通用码库中，可能是厂家私有码，请查对应车型手册", "source": None})
            top_kw = next((it for it in items if it["keywords"]), None)
            if top_kw:
                evidence.append({"kind": "keyword", "level": "info", "effect": "support",
                                 "text": f"描述里提到 “{'”“'.join(top_kw['keywords'][:4])}”，符合「{self.by_id[top_kw['id']]['name']}」的典型表述",
                                 "source": None})
        else:
            prior = {fid: 1 / len(self.ids) for fid in self.ids}
            if text:
                evidence.append({"kind": "text", "level": "info", "effect": "support",
                                 "text": "描述里没有识别到故障信息，先按下面的问题逐个确认", "source": None})

        # 车辆平台：告诉技师这次按什么平台调整了判断
        for n in vnotes:
            if n["note"]:
                evidence.append({"kind": "vehicle", "level": "info", "effect": "support",
                                 "text": f"按车辆平台「{n['dim']}：{n['label']}」调整：{n['note']}",
                                 "source": "工程经验（以车型手册为准）"})

        # 2) 数据流规则
        hits = self._apply_rules(m)
        order = {"fault": 0, "warn": 1, "ok": 2}
        for r, x in sorted(hits, key=lambda h: order.get(h[0]["level"], 3)):
            effect = "refute" if all(v < 1 for v in r["lr"].values()) else "support"
            evidence.append({"kind": "rule", "level": r["level"], "effect": effect,
                             "text": r["message"].replace("{value}", _fmt(x)), "source": r["source"]})

        # 3) 追问回答
        answered = []
        for qid, key in answers.items():
            q = self.q_by_id[qid]
            label = next((a["label"] for a in q["answers"] if a["key"] == key), "不确定")
            answered.append({"id": qid, "text": q["text"], "answer": key, "answer_label": label})

        post = self._posterior(prior, hits, answers, vmult)
        ranked = sorted(post.items(), key=lambda kv: -kv[1])
        top_id, top_p = ranked[0]
        top = self.by_id[top_id]

        nq = self._next_question(post, answers, m)
        conf = "高" if top_p >= 0.75 else "中" if top_p >= 0.45 else "低"
        # 什么时候可以出报告：
        #   有数据流命中或故障码 → 可以（这是硬证据，哪怕概率不高也值得给排查方向）
        #   否则首位概率要过门槛（光靠模糊描述 + 一两个问题不够）
        has_hard_evidence = bool(hits) or any(e["kind"] == "dtc" and e["level"] == "fault" for e in evidence)
        need_more = not has_hard_evidence and top_p < MIN_REPORT_PROB

        diag_id = db.save_diagnosis(self.conn, text, {k: v for k, v in m.items() if k in self.params},
                                    answers, top_id, round(top_p, 4))
        return {
            "ms": round((time.perf_counter() - t0) * 1000, 2),
            "ranker": self.ranker.name,
            "diagnosis_id": diag_id,
            "text": text,
            "text_used": text_used,
            "vehicle": chosen,
            "vehicle_notes": vnotes,
            "status": "need_more" if need_more else "ok",
            "candidates": [
                {"id": fid, "name": self.by_id[fid]["name"], "system": self.by_id[fid]["system"],
                 "severity": self.by_id[fid]["severity"], "hv_risk": self.by_id[fid]["hv_risk"], "prob": round(p, 4)}
                for fid, p in ranked[:5]
            ],
            "evidence": evidence,
            "derived": {k: {"name": DERIVED[k][0], "unit": DERIVED[k][1], "value": round(m[k], 2)} for k in DERIVED if k in m},
            "next_question": nq,
            "suggest_measure": self._suggest_measure(ranked, m),
            "answered": answered,
            "report": None if need_more else {
                "fault_id": top_id, "name": top["name"], "prob": round(top_p, 4), "confidence": conf,
                "hv_risk": top["hv_risk"], "symptoms": top["symptoms"], "causes": top["causes"],
                "steps": top["steps"], "safety": top["safety"], "refs": top["refs"],
                "notes": self.report_notes.get(top_id, []),
            },
            "similar_cases": [] if need_more else self.similar_cases(text if text_used else "", top_id),
        }
