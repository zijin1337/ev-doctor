"""
可选的大模型增强层。不配置也完全不影响诊断——概率、依据、追问全部由本地数据库和规则算出。

大模型只做两件"嘴和眼"的事，不参与判断：
  1. explain：把本地诊断结果讲成大白话（必须沿用本地结论和依据，不许改结论、不许编数值）
  2. vision ：拍诊断仪 / 仪表盘照片 → 识别读数和故障码，自动填进数据流表单

配置（环境变量优先，其次同目录 config.json），任何 OpenAI 兼容接口都行：
  EVDOC_API_BASE      例如 https://api.deepseek.com/v1
  EVDOC_API_KEY
  EVDOC_MODEL         例如 deepseek-chat
  EVDOC_VISION_MODEL  可选，能看图的模型，例如 qwen-vl-max；不填就用 EVDOC_MODEL
只依赖标准库 urllib。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from privacy import mask

HERE = os.path.dirname(os.path.abspath(__file__))


class LLMError(Exception):
    pass


CONFIG_PATH = os.path.join(HERE, "config.json")
CONFIG_KEYS = ("api_base", "api_key", "model", "vision_model")


def _read_file():
    # Windows 隐藏扩展名时，用户改名常常得到 config.json.json，也一并认
    for p in (CONFIG_PATH, CONFIG_PATH + ".json", CONFIG_PATH + ".txt"):
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8-sig") as f:  # utf-8-sig：兼容记事本保存带 BOM 的文件
                    return json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                print(f"[提示] {os.path.basename(p)} 格式不对，已忽略：{e}")
                return {}
    return {}


def save_config(new):
    """网页设置里保存。api_key 留空表示不改原来的 key。"""
    cur = _read_file()
    for k in CONFIG_KEYS:
        if k in new:
            v = str(new[k] or "").strip()
            if k == "api_key" and not v:
                continue
            cur[k] = v
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: cur.get(k, "") for k in CONFIG_KEYS}, f, ensure_ascii=False, indent=2)


def clear_key():
    cur = _read_file()
    cur["api_key"] = ""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: cur.get(k, "") for k in CONFIG_KEYS}, f, ensure_ascii=False, indent=2)


def masked(key):
    if not key:
        return ""
    return key[:3] + "****" + key[-4:] if len(key) > 10 else "****"


def public_config():
    """给前端看的配置：key 只显示首尾几位。"""
    cfg = load_config()
    env = any(os.environ.get(v) for v in ("EVDOC_API_BASE", "EVDOC_API_KEY", "EVDOC_MODEL"))
    return {"api_base": cfg["base"], "model": cfg["model"], "vision_model": cfg["vision_model"],
            "key_set": bool(cfg["key"]), "key_masked": masked(cfg["key"]), "ready": llm_ready(cfg),
            "from_env": env}


def _cfg_from(override=None):
    """测试 / 拉模型时优先用表单里刚填的值（还没保存也能测），key 留空就用已保存的。"""
    cfg = load_config()
    o = override or {}
    if o.get("api_base"):
        cfg["base"] = _norm_base(o["api_base"])
    if o.get("api_key"):
        cfg["key"] = o["api_key"].strip()
    for k_src, k_dst in (("model", "model"), ("vision_model", "vision_model")):
        if k_src in o and o[k_src] is not None:
            cfg[k_dst] = str(o[k_src]).strip()
    return cfg


def _friendly(e):
    """把常见错误翻译成能看懂的提示。"""
    s = str(e)
    if " 401" in s or "Unauthorized" in s or "invalid_api_key" in s or "Incorrect API key" in s:
        return "key 不对或已失效（401）"
    if " 402" in s or "Insufficient Balance" in s or "insufficient_quota" in s:
        return "账户余额不足（402），去平台充值"
    if " 403" in s:
        return "没有权限（403），检查 key 是否开通了这个模型"
    if " 404" in s:
        return "地址或模型名不对（404），检查是否少了 /v1，或模型名拼错"
    if " 429" in s:
        return "请求太频繁或额度用完（429），稍后再试"
    if "model" in s.lower() and ("not exist" in s.lower() or "not found" in s.lower() or "does not" in s.lower()):
        return "模型名不存在，点“获取模型”从列表里选"
    if "timed out" in s or "timeout" in s.lower():
        return "连接超时，检查网络或地址"
    if "Name or service not known" in s or "getaddrinfo" in s or "nodename" in s:
        return "域名解析失败，地址写错了或没联网"
    if "EOF" in s.upper() and "SSL" in s.upper() or "Connection reset" in s:
        return "连接被中断：国内直连 OpenAI 等海外接口通常不通，换国内平台或中转站地址"
    if "CERTIFICATE" in s.upper() or "SSL" in s.upper():
        return "SSL 证书校验失败，检查地址是否正确或网络是否被代理拦截"
    return s


def _with_v1(base):
    """地址里没有版本号（/v1、/v4、/compatible-mode/v1 之类）时，返回补上 /v1 的地址，否则返回 None。"""
    return None if re.search(r"/v\d+$", base) else base + "/v1"


def list_models(override=None):
    """GET {base}/models，返回模型 id 列表。大多数 OpenAI 兼容接口都支持。"""
    cfg = _cfg_from(override)
    if not cfg["base"] or not cfg["key"]:
        raise LLMError("先填接口地址和 key")
    bases = [cfg["base"]] + ([_with_v1(cfg["base"])] if _with_v1(cfg["base"]) else [])
    for i, base in enumerate(bases):
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + cfg["key"]})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            if e.code == 404 and i + 1 < len(bases):
                continue  # 没有 /v1 的地址 404 了，补上 /v1 再试
            if e.code == 404:
                raise LLMError("这个接口不支持获取模型列表（404），请手动填写模型名")
            raise LLMError(_friendly(f"接口返回 {e.code}：{detail}"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(_friendly(f"连不上：{getattr(e, 'reason', e)}"))
        except json.JSONDecodeError:
            if i + 1 < len(bases):
                continue
            raise LLMError("返回的不是 JSON，地址可能不对（是否少了 /v1？）")
    items = body.get("data") if isinstance(body, dict) else body
    if not isinstance(items, list):
        raise LLMError("返回格式不认识，请手动填写模型名")
    ids = sorted({(m.get("id") if isinstance(m, dict) else str(m)) for m in items} - {None, ""})
    if not ids:
        raise LLMError("模型列表是空的，检查 key 的权限")
    return ids


# 8×8 纯红色 PNG，用来测试模型能不能看图：回答里带"红"才算真的看到了
_TINY_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP4z8CAFWEXHbQSACj/P8Fu7N9hAAAAAElFTkSuQmCC"


def _probe(cfg, model, image=False):
    t0 = time.perf_counter()
    content = [{"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
               {"type": "image_url", "image_url": {"url": _TINY_PNG}}] if image else "只回复一个字：好"
    payload = {"model": model, "max_tokens": 10, "temperature": 0,
               "messages": [{"role": "user", "content": content}]}
    try:
        with _request(cfg, payload, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except LLMError as e:
        raise LLMError(_friendly(e))
    try:
        body = json.loads(raw)
        reply = body["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        raise LLMError("接口有响应，但不是 OpenAI 兼容格式（地址是否少了 /v1？）")
    if isinstance(reply, list):
        reply = "".join(x.get("text", "") for x in reply if isinstance(x, dict))
    reply = (reply or "").strip()
    if image:
        # 有些中转站对部分模型会悄悄丢掉图片，模型会回"我没看到图片"——这不算能看图。
        # 必须答出"红"才算看到了。放宽到"橙"会被瞎猜的模型骗过（实测 gpt-6-astra 没收到图也答"浅橙色"）。
        low = reply.lower()
        saw_color = any(w in low for w in ("红", "red", "crimson", "scarlet"))
        said_no_image = any(w in reply for w in ("没有看到", "看不到", "没看到", "未看到", "无法看到", "请上传", "重新上传")) \
            or any(w in low for w in ("no image", "don't see", "cannot see", "can't see", "not see"))
        if said_no_image or not saw_color:
            raise LLMError(f"{model} 收不到图片（回答：{reply[:30]}），换一个看图模型")
    return {"model": model, "ms": round((time.perf_counter() - t0) * 1000), "reply": reply[:30]}


def test_connection(override=None):
    """分别测文字模型和看图模型。返回每一项的结果，失败的项带 error。"""
    cfg = _cfg_from(override)
    if not cfg["base"] or not cfg["key"]:
        raise LLMError("先填接口地址和 key")
    if not cfg["model"]:
        raise LLMError("先填模型名，或点“获取模型”从列表里选")
    out = {}
    try:
        out["text"] = {"ok": True, **_probe(cfg, cfg["model"])}
    except LLMError as e:
        out["text"] = {"ok": False, "model": cfg["model"], "error": str(e)}
        alt = _with_v1(cfg["base"])
        if alt and ("404" in str(e) or "不是 OpenAI 兼容" in str(e)):
            try:  # 补上 /v1 再试，通了就提示用户改地址
                cfg = dict(cfg, base=alt)
                out["text"] = {"ok": True, **_probe(cfg, cfg["model"]), "fixed_base": alt}
            except LLMError:
                pass
    vm = cfg["vision_model"] or cfg["model"]
    if out["text"]["ok"] or cfg["vision_model"]:
        try:
            out["vision"] = {"ok": True, **_probe(cfg, vm, image=True)}
        except LLMError as e:
            msg = str(e)
            if not cfg["vision_model"]:
                msg = f"{vm} 不能看图，拍照识别需要另填一个看图模型"
            out["vision"] = {"ok": False, "model": vm, "error": msg}
    return out


def _norm_base(b):
    b = (b or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        b = b[: -len("/chat/completions")]
    if b.endswith("/models"):
        b = b[: -len("/models")]
    if b and not b.startswith(("http://", "https://")):
        b = "https://" + b
    return b.rstrip("/")


def load_config():
    cfg = _read_file()
    out = {
        "base": os.environ.get("EVDOC_API_BASE", cfg.get("api_base", "")).strip().rstrip("/"),
        "key": os.environ.get("EVDOC_API_KEY", cfg.get("api_key", "")).strip(),
        "model": os.environ.get("EVDOC_MODEL", cfg.get("model", "")).strip(),
        "vision_model": os.environ.get("EVDOC_VISION_MODEL", cfg.get("vision_model", "")).strip(),
    }
    if "把你的key" in out["key"]:  # 示例占位符，当作没填
        out["key"] = ""
    out["base"] = _norm_base(out["base"])  # 常见手误：末尾多写 /chat/completions、漏 https://
    return out


def llm_ready(cfg=None):
    cfg = cfg or load_config()
    return bool(cfg["base"] and cfg["key"] and cfg["model"])


def _request(cfg, payload, timeout=60):
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["key"]},
        method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise LLMError(f"接口返回 {e.code}：{detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"连不上大模型接口：{getattr(e, 'reason', e)}")


# ======================================================================
# 1. 大白话解释
# ======================================================================

EXPLAIN_SYSTEM = """你是新能源汽车维修门店里带徒弟的老师傅。你会收到本地诊断系统算好的结果：候选故障和概率、逐条判断依据（每条带出处）、技师回答过的问题，以及知识库里的排查步骤。

你的任务：把这份结果讲给一线技师或车主听。必须遵守：
1. 结论必须和系统的第一候选一致。不许改概率，也不许换成别的故障。如果你觉得证据之间有矛盾，只能在“注意”里点出来。
2. 只能使用给你的依据和知识库内容，不许编造故障码、数值或标准号。引用依据时写成【依据：……】。
3. 严格按下面的 Markdown 结构输出，总共不超过 350 字，口语化但要专业：

## 一句话结论
## 为什么这么判断
2~4 条，每条对应一条依据
## 现在先做什么
最多 3 步，先易后难、先低压后高压
## 注意
安全要求；证据有矛盾或信息不足时，说明还缺什么"""


def build_explain_prompt(result):
    r = result["report"]
    # 描述要发给外部大模型接口，先脱敏
    lines = ["【技师描述】" + (mask(result.get("text")) or "（没有文字描述，只录了数据流）"), "", "【系统候选】"]
    for c in result["candidates"][:3]:
        lines.append(f"- {c['name']}：{c['prob'] * 100:.1f}%")
    lines += ["", "【判断依据】"]
    if result["evidence"]:
        for e in result["evidence"]:
            tag = {"fault": "异常", "warn": "偏离", "ok": "正常", "info": "线索"}.get(e["level"], "")
            eff = "（用于排除）" if e["effect"] == "refute" else ""
            src = f"【依据：{e['source']}】" if e.get("source") else ""
            lines.append(f"- [{tag}]{eff} {e['text']} {src}")
    else:
        lines.append("- 只有文字描述，没有数据流")
    if result.get("answered"):
        lines += ["", "【已回答的问题】"]
        lines += [f"- {a['text']} → {a['answer_label']}" for a in result["answered"]]
    lines += ["", f"【知识库：{r['name']}】",
              "常见成因：" + "；".join(r["causes"]),
              "标准排查：" + "；".join(r["steps"]),
              "安全要求：" + r["safety"],
              "参考依据：" + "；".join(r["refs"])]
    return "\n".join(lines)


def stream_explain(result):
    """生成器，逐段吐出文字。出错抛 LLMError（只在第一段之前）或在末尾附错误说明。"""
    cfg = load_config()
    if not llm_ready(cfg):
        raise LLMError("未配置大模型")
    payload = {
        "model": cfg["model"], "stream": True, "temperature": 0.3,
        "messages": [{"role": "system", "content": EXPLAIN_SYSTEM},
                     {"role": "user", "content": build_explain_prompt(result)}],
    }
    resp = _request(cfg, payload)
    got = False
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    got = True
                    yield delta
    except (OSError, TimeoutError) as e:
        if not got:
            raise LLMError(f"大模型连接中断：{e}")
        yield f"\n\n> 连接中断，以上内容可能不完整。"


# ======================================================================
# 2. 拍照识别读数
# ======================================================================

VISION_PROMPT = """这是一张新能源汽车诊断仪数据流界面、故障码界面或仪表盘的照片。请识别其中的信息，只输出一个 JSON 对象，不要输出任何别的文字：
{
  "description": "一句中文客观描述：亮了哪些警告灯、屏幕上有什么提示文字",
  "dtc": ["照片里出现的故障码，例如 P0AA6，没有就空数组"],
  "measurements": {"参数id": 数值}
}
measurements 只能使用下面列出的参数 id，单位换算成表里的单位（例如绝缘电阻 2.5MΩ 要写成 2500，因为单位是 kΩ）；照片里没有的参数不要写，看不清的不要猜。
可用参数（id｜名称｜单位）：
%s"""


def _extract_json(s):
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise LLMError("模型没有返回可解析的结果")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        raise LLMError("模型返回的 JSON 格式不对")


def read_photo(data_url, params):
    cfg = load_config()
    if not llm_ready(cfg):
        raise LLMError("未配置大模型")
    plist = "\n".join(
        f"{p['id']}｜{p['name']}｜{p['unit'] or ('可选值：' + '/'.join(o['value'] for o in p['options']))}"
        for p in params)
    payload = {
        "model": cfg["vision_model"] or cfg["model"], "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT % plist},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
    }
    with _request(cfg, payload, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型返回格式异常")
    if isinstance(content, list):  # 少数接口返回分段内容
        content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
    data = _extract_json(content)
    return {
        "description": str(data.get("description", "")).strip(),
        "dtc": [str(c).upper() for c in data.get("dtc", []) if re.fullmatch(r"[PBCU][0-9A-F]{4}", str(c).upper())],
        "measurements": data.get("measurements") or {},
    }
