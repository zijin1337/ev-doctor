"""
电诊 EV-Doctor 桌面版（Qt / PySide6）。

    pip install PySide6-Essentials     # 只有桌面版需要；诊断核心和网页版仍然零依赖
    python gui.py                      # Windows 也可以双击 start_gui.bat

直接调用本地诊断引擎，不用起网页服务，断网可用；和网页版共用 data/evdoctor.db。
三个页面：诊断（描述 + 车辆平台 + 数据流 + 追问 + 报告 + 确认写回）、故障库、维修案例。
"""
import html
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFormLayout, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import db
import engine

NAVY, INK, GRAY, BLUE, ORANGE, GREEN = "#15243B", "#34465E", "#66727F", "#2F5AA8", "#EE6A1F", "#2E7D4F"
LEVEL_COLOR = {"fault": "#B3261E", "warn": ORANGE, "ok": GREEN, "info": "#9AA5B1"}

SAMPLES = [
    "早上按启动，仪表亮了但 READY 一直不出来，诊断仪读到 P0A0A，前两天刚换过空调压缩机",
    "梅雨天停了一晚，第二天仪表报绝缘故障，车子上不了高压，太阳出来又好了",
    "国标直流桩插上枪一直转圈，最后提示车辆通信失败，家里交流充电正常",
    "车在地库放了半个月，遥控钥匙没反应，仪表全黑，动力电池还有 70% 的电",
    "三伏天跑高速，仪表弹出电机过热请减速，动力明显变弱",
    "车上不了高压，仪表报动力系统故障",
]
DEMOS = [
    ("绝缘故障", "下雨后车上不了高压，仪表报绝缘故障", {"pack_voltage": 380, "insulation_kohm": 25, "hvil_state": "closed"}),
    ("DC-DC 故障", "", {"lv_voltage_off": 12.1, "lv_voltage_ready": 12.3}),
    ("交流充不进", "家用交流桩插上枪没反应，充不进电", {"ac_cp_v": 9, "ac_cc_ohm": 680}),
]

QSS = """
QWidget { color: #15243B; font-size: 10pt; }
QMainWindow, QWidget#root { background: #EEF2F7; }
QWidget#pane { background: transparent; }
QScrollArea { background: transparent; border: none; }
QFrame#header { background: #FFFFFF; border-bottom: 1px solid #DCE4F2; }
QFrame#card { background: #FFFFFF; border: 1px solid #DCE4F2; border-radius: 10px; }
QFrame#soft { background: #F4F7FC; border: 1px solid #DCE4F2; border-radius: 10px; }
QFrame#warn { background: #FDEBDD; border: none; border-radius: 10px; }

QLabel#logo { font-size: 20pt; font-weight: bold; color: #15243B; }
QLabel#logo2 { font-size: 14pt; font-weight: bold; color: #2F5AA8; }
QLabel#title { font-size: 18pt; font-weight: bold; color: #15243B; }
QLabel#h2 { font-size: 11pt; font-weight: bold; color: #15243B; }
QLabel#h3 { font-size: 10pt; font-weight: bold; color: #66727F; padding-top: 6px; }
QLabel#group { font-size: 9pt; font-weight: bold; color: #66727F; padding-top: 8px; }
QLabel#muted { color: #66727F; }
QLabel#small { color: #8792A0; font-size: 9pt; }
QLabel#src { color: #2E7D4F; font-size: 9pt; }
QLabel#cand { font-size: 11pt; font-weight: bold; color: #15243B; }
QLabel#cand-dim { font-size: 11pt; font-weight: bold; color: #9AA5B1; }
QLabel#pct { font-size: 16pt; font-weight: bold; color: #15243B; }
QLabel#q { font-size: 12pt; font-weight: bold; color: #15243B; }
QLabel#vnote { color: #2F5AA8; font-size: 9pt; }
QLabel#tag-hv { color: #EE6A1F; background: #FDEBDD; border-radius: 4px; padding: 1px 6px; font-size: 9pt; }
QLabel#note { background: #E4EBF7; color: #15243B; border-radius: 6px; padding: 8px 10px; }
QLabel#safety { background: #FDEBDD; color: #15243B; border-radius: 6px; padding: 8px 10px; }
QLabel#rname { font-size: 15pt; font-weight: bold; color: #15243B; }
QLabel#conf-高 { color: #2E7D4F; background: #E3F1E8; border-radius: 4px; padding: 2px 8px; font-size: 9pt; }
QLabel#conf-中 { color: #B26A00; background: #FDEBDD; border-radius: 4px; padding: 2px 8px; font-size: 9pt; }
QLabel#conf-低 { color: #B3261E; background: #FBE3E1; border-radius: 4px; padding: 2px 8px; font-size: 9pt; }
QLabel#sample { color: #34465E; padding: 8px 2px; border-bottom: 1px solid #E1E7EF; }
QLabel#sample:hover { color: #2F5AA8; }

QLineEdit, QPlainTextEdit, QComboBox {
  background: #FFFFFF; border: 1px solid #CBD3DA; border-radius: 6px; padding: 5px 9px;
  selection-background-color: #DCE4F2; selection-color: #15243B;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border: 1px solid #2F5AA8; }
QPlainTextEdit { font-size: 11pt; padding: 8px 10px; }
QComboBox QAbstractItemView { background: #FFFFFF; border: 1px solid #CBD3DA; outline: 0;
  selection-background-color: #E4EBF7; selection-color: #15243B; }

QPushButton { border: none; border-radius: 6px; }
QPushButton#primary { background: #15243B; color: #FFFFFF; padding: 10px 20px; font-size: 11pt; font-weight: bold; }
QPushButton#primary:hover { background: #2F5AA8; }
QPushButton#primary:disabled { background: #2E7D4F; color: #FFFFFF; }
QPushButton#ghost { background: #E4EBF7; color: #2F5AA8; padding: 7px 14px; }
QPushButton#ghost:hover { background: #D5E0F2; }
QPushButton#answer { background: #FFFFFF; color: #2F5AA8; border: 1px solid #9EB0CF; padding: 8px 14px;
  font-weight: bold; text-align: left; }
QPushButton#answer:hover { border: 1px solid #2F5AA8; background: #F4F7FC; }

QProgressBar { background: #DCE4F2; border: none; border-radius: 4px; }
QProgressBar::chunk { background: #2F5AA8; border-radius: 4px; }

QTabWidget::pane { border: none; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab { background: transparent; color: #66727F; padding: 8px 22px; margin-right: 4px; font-size: 11pt;
  border: none; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #15243B; font-weight: bold; border-bottom: 3px solid #2F5AA8; }
QTabBar::tab:hover { color: #15243B; }

QTableWidget, QTreeWidget, QTextBrowser { background: #FFFFFF; border: 1px solid #DCE4F2; border-radius: 10px;
  gridline-color: #EEF2F7; selection-background-color: #E4EBF7; selection-color: #15243B; }
QTreeWidget::item { padding: 6px 4px; }
QHeaderView::section { background: #F4F7FC; color: #15243B; border: none; border-bottom: 1px solid #DCE4F2;
  padding: 7px 8px; font-weight: bold; }
QSplitter::handle { background: transparent; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #C5CFDC; border-radius: 4px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #9EB0CF; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #C5CFDC; border-radius: 4px; min-width: 32px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip { background: #15243B; color: #FFFFFF; border: none; padding: 6px; }
"""


def pct(p):
    v = p * 100
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}" if v >= 1 else "<1"


def label(text, obj=None, wrap=True, select=False):
    lab = QLabel(text)
    lab.setTextFormat(Qt.PlainText)
    lab.setWordWrap(wrap)
    if obj:
        lab.setObjectName(obj)
    if select:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


def card(obj="card", margins=(18, 14, 18, 16)):
    frame = QFrame()
    frame.setObjectName(obj)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(*margins)
    lay.setSpacing(8)
    return frame, lay


def button(text, obj, slot):
    b = QPushButton(text)
    b.setObjectName(obj)
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(slot)
    return b


class ClickLabel(QLabel):
    clicked = Signal()

    def __init__(self, text):
        super().__init__(text)
        self.setWordWrap(True)
        self.setObjectName("sample")
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        self.clicked.emit()


def scroll(inner):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    inner.setObjectName("pane")
    area.setWidget(inner)
    return area


class MainWindow(QMainWindow):
    def __init__(self, db_path=None):
        super().__init__()
        self.setWindowTitle("电诊 EV-Doctor —— 新能源汽车故障诊断")
        self.resize(1400, 900)
        self.conn = db.init_db(db_path) if db_path else db.init_db()
        self.eng = engine.Engine(self.conn)
        self.params = db.get_params(self.conn)
        self.faults = db.get_faults(self.conn)
        self.name2id = {f["name"]: f["id"] for f in self.faults}
        self.answers, self.last, self.hidden = {}, None, set()

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._header())
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 10, 16, 14)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._diag_page(), "诊断")
        self.tabs.addTab(self._lib_page(), "故障库")
        self.tabs.addTab(self._cases_page(), "维修案例")
        self.tabs.currentChanged.connect(self._on_tab)
        bl.addWidget(self.tabs)
        v.addWidget(body, 1)
        self.refresh_stats()

    # ------------------------------------------------------------------ 顶栏
    def _header(self):
        bar = QFrame()
        bar.setObjectName("header")
        h = QHBoxLayout(bar)
        h.setContentsMargins(24, 12, 24, 12)
        h.setSpacing(10)
        h.addWidget(label("电诊", "logo", wrap=False))
        h.addWidget(label("EV-Doctor", "logo2", wrap=False))
        h.addSpacing(10)
        h.addWidget(label("桌面版 · 纯本地 · 断网可用", "muted", wrap=False))
        h.addStretch(1)
        self.stats = label("", "muted", wrap=False)
        h.addWidget(self.stats)
        return bar

    def refresh_stats(self):
        c = db.counts(self.conn)
        self.stats.setText(f"知识库 {c['fault_count']} 类  ·  规则 {c['rule_count']} 条  ·  "
                           f"追问 {c['question_count']} 个  ·  案例 {c['case_count']} 条")

    def _on_tab(self, i):
        if i == 1:
            self.load_lib()
        elif i == 2:
            self.load_cases()

    # ------------------------------------------------------------------ 诊断页
    def _diag_page(self):
        inner = QWidget()
        L = QVBoxLayout(inner)
        L.setContentsMargins(2, 8, 14, 18)
        L.setSpacing(12)
        L.addWidget(label("车出了什么毛病？", "title"))
        L.addWidget(label("写下故障现象或故障码；选好车辆平台、再录几项数据流，会准得多。", "muted"))
        self.q = QPlainTextEdit()
        self.q.setPlaceholderText("例如：早上按启动，仪表亮了但 READY 一直不出来，诊断仪读到 P0A0A")
        self.q.setFixedHeight(100)
        L.addWidget(self.q)
        QShortcut(QKeySequence("Ctrl+Return"), self.q, activated=lambda: self.diagnose(new=True))

        # 车辆平台
        vc, vl = card()
        vl.addWidget(label("车辆平台", "h2"))
        vl.addWidget(label("看铭牌或维修手册确定；不确定就保持默认，按通用阈值判断。", "small"))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.vcombo = {}
        for d in self.eng.vehicle:
            cb = QComboBox()
            for o in d["options"]:
                cb.addItem(o["label"], o)
            cb.currentIndexChanged.connect(self._vehicle_changed)
            form.addRow(label(d["name"], wrap=False), cb)
            self.vcombo[d["id"]] = cb
        self.model = QLineEdit()
        self.model.setPlaceholderText("如：秦PLUS EV（可选，确认时记进案例）")
        form.addRow(label("车型", wrap=False), self.model)
        vl.addLayout(form)
        L.addWidget(vc)

        # 示例
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(label("填入示例", "small", wrap=False))
        for name, text, m in DEMOS:
            row.addWidget(button(name, "ghost", lambda _=False, t=text, m=m: self.load_demo(t, m)))
        row.addStretch(1)
        L.addLayout(row)

        # 数据流
        mc, ml = card()
        ml.addWidget(label("录入数据流（没测的留空）", "h2"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        self.minputs, self.mrows = {}, {}
        r, group = 0, None
        for p in self.params:
            if p["group"] != group:
                group = p["group"]
                grid.addWidget(label(group, "group"), r, 0, 1, 3)
                r += 1
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(0)
            cl.addWidget(label(p["name"]))
            cl.addWidget(label("正常：" + p["normal"], "small"))
            cell.setToolTip(p["hint"])
            if p["type"] == "enum":
                w = QComboBox()
                w.addItem("未测", "")
                for o in p["options"]:
                    w.addItem(o["label"], o["value"])
            else:
                w = QLineEdit()
                w.setAlignment(Qt.AlignRight)
                w.returnPressed.connect(lambda: self.diagnose(new=True))
            w.setFixedWidth(108)
            unit = label(p["unit"] or "", "small", wrap=False)
            unit.setFixedWidth(30)
            grid.addWidget(cell, r, 0)
            grid.addWidget(w, r, 1, Qt.AlignTop)
            grid.addWidget(unit, r, 2, Qt.AlignTop)
            self.minputs[p["id"]] = (p, w)
            self.mrows[p["id"]] = (cell, w, unit)
            r += 1
        ml.addLayout(grid)
        L.addWidget(mc)

        act = QHBoxLayout()
        act.addWidget(button("开始诊断", "primary", lambda: self.diagnose(new=True)), 1)
        act.addWidget(button("重新开始", "ghost", self.reset))
        L.addLayout(act)
        self.hint = label("按 Ctrl + Enter 也能开始诊断", "small")
        L.addWidget(self.hint)

        L.addSpacing(6)
        L.addWidget(label("试试这些真实场景", "h2"))
        for s in SAMPLES:
            c = ClickLabel(s)
            c.clicked.connect(lambda s=s: self.load_sample(s))
            L.addWidget(c)
        L.addStretch(1)

        self.res_inner = QWidget()
        self.R = QVBoxLayout(self.res_inner)
        self.R.setContentsMargins(14, 8, 10, 18)
        self.R.setSpacing(12)
        self.right = scroll(self.res_inner)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        left = scroll(inner)
        left.setMinimumWidth(460)
        split.addWidget(left)
        split.addWidget(self.right)
        split.setSizes([520, 880])
        self.placeholder()
        return split

    def clear_results(self):
        while self.R.count():
            item = self.R.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)  # 立刻从界面上摘掉，避免新旧结果叠在一起
                w.deleteLater()
        self.right.verticalScrollBar().setValue(0)

    def placeholder(self):
        self.clear_results()
        c, cl = card(margins=(30, 70, 30, 70))
        t = label("诊断结果会显示在这里", "h2")
        t.setAlignment(Qt.AlignCenter)
        cl.addWidget(t)
        s = label("系统根据描述、车辆平台和数据流算出各类故障的概率，每条判断都标明依据的国标或经验。\n"
                  "还分不清的时候，它一次只问你一个最关键的问题。", "muted")
        s.setAlignment(Qt.AlignCenter)
        cl.addWidget(s)
        self.R.addWidget(c)
        self.R.addStretch(1)

    def _vehicle_changed(self):
        hide = set()
        for cb in self.vcombo.values():
            hide.update(cb.currentData().get("hide_params", []))
        for pid, widgets in self.mrows.items():
            for w in widgets:
                w.setVisible(pid not in hide)
            if pid in hide:
                self._clear_measure(pid)
        self.hidden = hide
        if self.last:
            self.diagnose()

    def read_vehicle(self):
        return {dim: cb.currentData()["value"] for dim, cb in self.vcombo.items() if cb.currentData()["value"]}

    def read_measurements(self):
        m = {}
        for pid, (p, w) in self.minputs.items():
            if pid in self.hidden:
                continue
            v = w.currentData() if p["type"] == "enum" else w.text().strip()
            if v:
                m[pid] = v  # 数字由引擎校验，填错会提示
        return m

    def _clear_measure(self, pid):
        p, w = self.minputs[pid]
        if p["type"] == "enum":
            w.setCurrentIndex(0)
        else:
            w.clear()

    def set_measurements(self, m):
        for pid in self.minputs:
            self._clear_measure(pid)
        for pid, v in m.items():
            p, w = self.minputs[pid]
            if p["type"] == "enum":
                w.setCurrentIndex(max(w.findData(v), 0))
            else:
                w.setText(str(v))

    def load_demo(self, text, m):
        self.q.setPlainText(text)
        self.set_measurements(m)
        self.diagnose(new=True)

    def load_sample(self, s):
        self.q.setPlainText(s)
        self.set_measurements({})
        self.diagnose(new=True)

    def reset(self):
        self.q.clear()
        self.set_measurements({})
        self.answers, self.last = {}, None
        self.hint.setText("已清空，写下新的故障现象开始诊断。")
        self.placeholder()

    def diagnose(self, new=False):
        if new:
            self.answers = {}
        try:
            d = self.eng.diagnose(self.q.toPlainText().strip(), self.read_measurements(), self.answers, self.read_vehicle())
        except ValueError as e:
            self.hint.setText(str(e))
            self.hint.setStyleSheet("color: #B3261E;")
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "诊断出错", str(e))
            return
        self.hint.setText("按 Ctrl + Enter 也能开始诊断")
        self.hint.setStyleSheet("")
        self.last = d
        self.render(d)
        self.refresh_stats()

    def answer(self, qid, key):
        self.answers[qid] = key
        self.diagnose()

    # ------------------------------------------------------------------ 结果
    def render(self, d):
        self.clear_results()
        r = d["report"]

        top, tl = card()
        if d["status"] == "need_more":
            warn, wl = card("warn", (14, 10, 14, 10))
            wl.addWidget(label("信息还不够，先别下结论", "h2"))
            wl.addWidget(label("描述里看不出具体现象，说说仪表亮了什么灯、哪一步卡住了，或者直接回答下面的问题。"
                               if d["text"] and not d["text_used"] else
                               "各类故障的可能性还差不多。回答下面的问题，或者录入几项数据流，系统才会给出排查方向。"))
            tl.addWidget(warn)
        else:
            h = QHBoxLayout()
            h.addWidget(label("最可能的故障", "h2", wrap=False))
            h.addStretch(1)
            h.addWidget(label(f"用时 {d['ms']:.1f} ms", "small", wrap=False))
            tl.addLayout(h)
        if d["vehicle_notes"]:
            tl.addWidget(label("已按车辆平台调整：" + " · ".join(n["label"] for n in d["vehicle_notes"]), "vnote"))
        for c in d["candidates"][:3]:
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addWidget(label(c["name"], "cand" if c["prob"] >= 0.05 else "cand-dim", wrap=False))
            line.addWidget(label(c["system"], "small", wrap=False))
            if c["hv_risk"]:
                line.addWidget(label("涉及高压", "tag-hv", wrap=False))
            line.addStretch(1)
            line.addWidget(label(pct(c["prob"]) + "%", "pct", wrap=False))
            tl.addSpacing(4)
            tl.addLayout(line)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(max(int(c["prob"] * 1000), 6))
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            tl.addWidget(bar)
        self.R.addWidget(top)

        q = d["next_question"]
        if q or d["answered"]:
            qc, ql = card("soft")
            if q:
                ql.addWidget(label("还需要确认一件事", "small"))
                ql.addWidget(label(q["text"], "q", select=True))
                for a in q["answers"] + [{"key": "unknown", "label": "不确定"}]:
                    ql.addWidget(button(a["label"], "answer", lambda _=False, k=a["key"], qid=q["id"]: self.answer(qid, k)))
            for a in d["answered"]:
                ql.addWidget(label(f"已答：{a['text']}　→　{a['answer_label']}", "small"))
            self.R.addWidget(qc)

        if d["suggest_measure"]:
            sc, sl = card("soft")
            sl.addWidget(label("要进一步区分，建议测一下：", "h3"))
            for s in d["suggest_measure"]:
                sl.addWidget(label(f"•  {s['name']}：{s['hint']}，用来判断是否「{s['for']}」"))
            self.R.addWidget(sc)

        if r and r["hv_risk"]:
            hv, hl = card("warn", (16, 12, 16, 12))
            hl.addWidget(label("⚠ 排查会接触高压部件。非专业人员请不要自行拆修，交给持证技师按维修手册操作。", "h2"))
            self.R.addWidget(hv)

        if r:
            self._evidence(d)
            self._report(r)
            if d["similar_cases"]:
                self._similar(d["similar_cases"])
            self._confirm_form(d)
        self.R.addStretch(1)

    def _evidence(self, d):
        ev, el = card()
        el.addWidget(label("判断依据", "h2"))
        items = ([e for e in d["evidence"] if e["effect"] != "refute"] +
                 [e for e in d["evidence"] if e["effect"] == "refute"])
        if not items:
            el.addWidget(label("只根据描述做了初判。录入数据流或回答上面的问题，依据会更充分。", "muted"))
        for e in items:
            row = QHBoxLayout()
            row.setSpacing(10)
            dot = label("●", wrap=False)
            dot.setStyleSheet(f"color: {LEVEL_COLOR.get(e['level'], '#9AA5B1')}; font-size: 9pt;")
            row.addWidget(dot, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(label(e["text"] + ("　【用于排除】" if e["effect"] == "refute" else ""), select=True))
            if e.get("source"):
                col.addWidget(label("依据：" + e["source"], "src"))
            row.addLayout(col, 1)
            el.addLayout(row)
        if d["derived"]:
            el.addWidget(label("换算值：" + "　".join(f"{v['name']} {v['value']} {v['unit']}" for v in d["derived"].values()), "small"))
        self.R.addWidget(ev)

    def _report(self, r):
        rp, rl = card()
        rl.addWidget(label("诊断报告", "h2"))
        head = QHBoxLayout()
        head.addWidget(label(r["name"], "rname", wrap=False))
        head.addWidget(label(f"置信度{r['confidence']}", f"conf-{r['confidence']}", wrap=False))
        head.addStretch(1)
        rl.addLayout(head)
        for title, lines in (("典型现象", [r["symptoms"]]), ("常见成因", ["•  " + x for x in r["causes"]]),
                             ("排查步骤", [f"{i}. {x}" for i, x in enumerate(r["steps"], 1)])):
            rl.addWidget(label(title, "h3"))
            for x in lines:
                rl.addWidget(label(x, select=True))
        for note in r.get("notes", []):
            rl.addWidget(label("ⓘ " + note, "note"))
        rl.addWidget(label("安全提示", "h3"))
        rl.addWidget(label(r["safety"], "safety"))
        if r["refs"]:
            rl.addWidget(label("参考依据", "h3"))
            for x in r["refs"]:
                rl.addWidget(label(x, "small"))
        self.R.addWidget(rp)

    def _similar(self, cases):
        sc, sl = card()
        sl.addWidget(label("相似维修案例", "h2"))
        for c in cases:
            meta = "　".join(x for x in ((c["created_at"] or "")[:10], c["fault_name"], c["vehicle"],
                                        "本店确认" if c["source"] == "confirmed" else "案例库",
                                        f"相似度 {c['sim'] * 100:.0f}%" if c.get("sim") is not None else "") if x)
            sl.addWidget(label(meta, "small"))
            sl.addWidget(label(c["text"], select=True))
            if c["resolution"]:
                sl.addWidget(label("处理：" + c["resolution"], "muted"))
        self.R.addWidget(sc)

    def _confirm_form(self, d):
        cf, cl = card("soft")
        cl.addWidget(label("修好了？确认最终故障", "h2"))
        cl.addWidget(label("确认后这次诊断会存进本地案例库（入库前自动脱敏），以后遇到相似描述会认得更准。", "small"))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        fault = QComboBox()
        fault.addItems([f["name"] for f in self.faults])
        fault.setCurrentText(d["report"]["name"])
        fix, car = QLineEdit(), QLineEdit(self.model.text().strip())
        fix.setPlaceholderText("例如：更换压缩机高压插头密封圈，绝缘恢复到 2 MΩ")
        car.setPlaceholderText("车辆（可选）")
        form.addRow(label("最终故障", wrap=False), fault)
        form.addRow(label("处理措施", wrap=False), fix)
        form.addRow(label("车辆", wrap=False), car)
        text = None
        if not d["text"]:
            text = QLineEdit()
            text.setPlaceholderText("补一句故障现象（必填）")
            form.addRow(label("故障现象", wrap=False), text)
        cl.addLayout(form)
        row = QHBoxLayout()
        msg = label("", "small")
        btn = QPushButton("确认并存入案例库")
        btn.setObjectName("primary")
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.confirm(fault, fix, car, text, btn, msg))
        row.addWidget(btn)
        row.addWidget(msg, 1)
        cl.addLayout(row)
        self.R.addWidget(cf)

    def confirm(self, fault, fix, car, text, btn, msg):
        t = text.text().strip() if text is not None else None
        if text is not None and not t:
            msg.setText("请补一句故障现象")
            return
        try:
            self.eng.confirm(self.last["diagnosis_id"], self.name2id[fault.currentText()], fix.text(), car.text(), t)
        except ValueError as e:
            msg.setText(str(e))
            return
        n = db.counts(self.conn)["case_count"]
        btn.setText(f"已存入，第 {n} 条案例")
        btn.setEnabled(False)
        msg.setText("下次遇到相似描述会认得更准。")
        self.refresh_stats()

    # ------------------------------------------------------------------ 故障库
    def _lib_page(self):
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.lib_tree = QTreeWidget()
        self.lib_tree.setHeaderHidden(True)
        self.lib_tree.setIndentation(14)
        self.lib_tree.itemSelectionChanged.connect(self.show_fault)
        self.lib_view = QTextBrowser()
        self.lib_view.document().setDefaultStyleSheet(
            "h1{color:#15243B;font-size:18pt;} h3{color:#66727F;font-size:10pt;margin-top:14px;} "
            ".m{color:#66727F;} .hv{color:#EE6A1F;} .safe{background:#FDEBDD;color:#15243B;} .c{color:#66727F;font-size:9pt;}")
        self.lib_view.setStyleSheet("QTextBrowser { padding: 14px 18px; }")
        split.addWidget(self.lib_tree)
        split.addWidget(self.lib_view)
        split.setSizes([360, 1000])
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 10, 0, 0)
        pl.addWidget(split)
        self.load_lib()
        return page

    def load_lib(self):
        current = self.lib_tree.currentItem()
        keep = current.data(0, Qt.UserRole) if current else None
        self.faults = db.get_faults(self.conn)
        self.lib_tree.clear()
        groups, first, target = {}, None, None
        for f in self.faults:
            if f["system"] not in groups:
                g = QTreeWidgetItem([f["system"]])
                fnt = g.font(0)
                fnt.setBold(True)
                g.setFont(0, fnt)
                g.setFlags(Qt.ItemIsEnabled)
                self.lib_tree.addTopLevelItem(g)
                groups[f["system"]] = g
            it = QTreeWidgetItem([f"{f['name']}（{f['case_count']}）"])
            it.setData(0, Qt.UserRole, f["id"])
            groups[f["system"]].addChild(it)
            first = first or it
            if f["id"] == keep:
                target = it
        self.lib_tree.expandAll()
        self.lib_tree.setCurrentItem(target or first)

    def show_fault(self):
        it = self.lib_tree.currentItem()
        fid = it.data(0, Qt.UserRole) if it else None
        f = next((x for x in self.faults if x["id"] == fid), None)
        if not f:
            return
        e = html.escape
        sev = {"high": "严重", "medium": "中等", "low": "轻微"}.get(f["severity"], f["severity"])
        out = [f"<h1>{e(f['name'])}</h1><p class='m'>{e(f['system'])}　严重程度：{sev}"
               + ("　<span class='hv'>涉及高压</span>" if f["hv_risk"] else "") + "</p>"]
        if f["dtc"]:
            out.append(f"<p class='m'>故障码：{e('  '.join(f['dtc']))}</p>")
        out.append(f"<p class='m'>常见说法：{e('、'.join(f['keywords']))}</p>")
        out.append(f"<h3>典型现象</h3><p>{e(f['symptoms'])}</p>")
        out.append("<h3>常见成因</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in f["causes"]) + "</ul>")
        out.append("<h3>排查步骤</h3><ol>" + "".join(f"<li>{e(x)}</li>" for x in f["steps"]) + "</ol>")
        out.append(f"<h3>安全提示</h3><p class='safe'>{e(f['safety'])}</p>")
        if f["refs"]:
            out.append("<h3>参考依据</h3>" + "".join(f"<p class='m'>{e(x)}</p>" for x in f["refs"]))
        cases = db.get_cases(self.conn, fault_id=f["id"], limit=50)
        out.append(f"<h3>维修案例（{len(cases)}）</h3>")
        for c in cases:
            meta = "　".join(x for x in ((c["created_at"] or "")[:10], c["vehicle"],
                                        "本店确认" if c["source"] == "confirmed" else "案例库") if x)
            out.append(f"<p class='c'>{e(meta)}</p><p>{e(c['text'])}</p>"
                       + (f"<p class='c'>处理：{e(c['resolution'])}</p>" if c["resolution"] else ""))
        self.lib_view.setHtml("".join(out))

    # ------------------------------------------------------------------ 维修案例
    def _cases_page(self):
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 12, 0, 0)
        pl.setSpacing(10)
        bar = QHBoxLayout()
        bar.addWidget(label("维修案例", "h2", wrap=False))
        bar.addSpacing(12)
        self.case_q = QLineEdit()
        self.case_q.setPlaceholderText("搜描述、处理措施或车型")
        self.case_q.setFixedWidth(300)
        self.case_q.returnPressed.connect(self.load_cases)
        self.case_f = QComboBox()
        self.case_f.addItem("全部故障", "")
        for f in self.faults:
            self.case_f.addItem(f["name"], f["id"])
        self.case_f.currentIndexChanged.connect(self.load_cases)
        bar.addWidget(self.case_q)
        bar.addWidget(self.case_f)
        bar.addWidget(button("搜索", "ghost", self.load_cases))
        bar.addStretch(1)
        pl.addLayout(bar)
        self.case_table = self._table(["日期", "故障", "车辆", "现象描述", "处理措施", "来源"], stretch=(3, 4))
        pl.addWidget(self.case_table, 3)
        pl.addWidget(label("最近的诊断记录", "h2"))
        self.hist_table = self._table(["时间", "描述", "初判", "概率", "最终确认"], stretch=(1,))
        pl.addWidget(self.hist_table, 2)
        return page

    def _table(self, headers, stretch):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setAlternatingRowColors(False)
        t.setShowGrid(False)
        t.setWordWrap(True)
        hh = t.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for i in range(len(headers)):
            hh.setSectionResizeMode(i, QHeaderView.Stretch if i in stretch else QHeaderView.ResizeToContents)
        return t

    def _fill(self, table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                table.setItem(i, j, QTableWidgetItem(str(v)))
        table.resizeRowsToContents()

    def load_cases(self):
        cases = db.get_cases(self.conn, self.case_q.text().strip()[:100], self.case_f.currentData() or "", 300)
        self._fill(self.case_table, [((c["created_at"] or "")[:10], c["fault_name"], c["vehicle"] or "", c["text"],
                                      c["resolution"] or "", "本店确认" if c["source"] == "confirmed" else "案例库")
                                     for c in cases])
        hist = db.get_history(self.conn, 100)
        self._fill(self.hist_table, [(h["created_at"], h["text"] or "（只录了数据流）", h["top_name"] or "",
                                      f"{(h['top_prob'] or 0) * 100:.0f}%", h["confirmed_name"] or "") for h in hist])


def make_app(argv=None):
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setStyle("Fusion")
    families = set(QFontDatabase.families())
    family = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC") if f in families), None)
    if family:
        app.setFont(QFont(family, 10))
    pal = app.palette()
    pal.setColor(QPalette.Window, QColor("#EEF2F7"))
    pal.setColor(QPalette.Base, QColor("#FFFFFF"))
    pal.setColor(QPalette.Text, QColor(NAVY))
    pal.setColor(QPalette.WindowText, QColor(NAVY))
    pal.setColor(QPalette.ButtonText, QColor(NAVY))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
    return app


def main():
    app = make_app()
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
