"""
微信个人公众号 + 智能日程助手后端
Flask 服务，支持：微信消息 → LLM 分类解析 → 存储 → 回复 + 前端 API
"""
import json
import hashlib
import time
import re
import os
import gzip
import base64
import struct
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, request, Response, send_from_directory
import requests
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

app = Flask(__name__)

# ============= 配置 =============
WECHAT_TOKEN = os.environ.get("WECHAT_TOKEN", "your_wechat_token_here")

# 企业微信配置
WEWORK_CORP_ID = os.environ.get("WEWORK_CORP_ID", "wwca37750211c7f64c")
WEWORK_TOKEN = os.environ.get("WEWORK_TOKEN", "pF61CT3fSmV6t4bY")
WEWORK_AES_KEY = os.environ.get("WEWORK_AES_KEY", "clUIB1tVpc3dRnZETPOR6JoFesfiPYbOYSvlBd69NDw")
WEWORK_AGENT_ID = os.environ.get("WEWORK_AGENT_ID", "1000002")
WEWORK_SECRET = os.environ.get("WEWORK_SECRET", "yEIAUEESIvCi_0Z-L0axTrNAfAghr8cP8sFkUoTTQPY")

LLM_CONFIG = {
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-bf51236eafd94551916ba18b5854098b",
    "model": "deepseek-chat",
}

TASKS_FILE = "tasks.json"
TZ = timezone(timedelta(hours=8))


def safe_iso(s):
    """安全解析 ISO 时间，失败返回 epoch"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime(2000, 1, 1, tzinfo=TZ)

# 默认提醒规则（分钟）
DEFAULT_REMINDERS = {
    "作业限期": [10080, 4320],   # 7天, 3天
    "会议安排": [2880, 120],     # 2天, 2小时
    "信息提交": [2880, 120],     # 2天, 2小时
    "生活琐事": [720],           # 12小时
    "其他": [],
}

# ============= 任务存储（按用户 OpenID 隔离） =============

_all_tasks: dict[str, list] = {}  # {openid: [task, ...]}


def load_all() -> dict:
    global _all_tasks
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _all_tasks = data.get("user_tasks", {})
            if isinstance(data, list):
                _all_tasks = {"_legacy_": data}
                save_all()
        except Exception:
            _all_tasks = {}
    return _all_tasks


def save_all():
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"user_tasks": _all_tasks}, f, ensure_ascii=False, indent=2)


def get_user_tasks(openid: str) -> list:
    if openid not in _all_tasks:
        _all_tasks[openid] = []
    return _all_tasks[openid]


def get_all_tasks_flat() -> list:
    result = []
    for uid, tasks in _all_tasks.items():
        for t in tasks:
            t_copy = dict(t)
            t_copy["_user"] = uid[:10] + ".."
            result.append(t_copy)
    return result


# ============= LLM 分析 =============

SYSTEM_PROMPT = """你是一个同时具备任务管理和聊天能力的助手。

先判断用户输入属于哪一类：
- intent="task"：用户自己在安排要做的事，主语是"我"或隐含自己去做。比如"明天开会""下周五交报告""去超市买东西"。即使含时间词，也是给自己安排日程。
- intent="chat"：用户在提问、求助、闲聊、让你帮忙做事、讨论话题。比如"帮我写论文""Python怎么学""推荐本书""你好"。**用户让你帮忙做某事=chat，不是task。**

=== intent="task" 时返回： ===
{"intent":"task","taskType":"deadline","label":"5-12字标签","category":"会议安排|作业限期|信息提交|生活琐事|其他","priority":1-5,"notes":"用户原文","hasDateTime":true/false,"dateTime":"时间描述"或null,"isoTime":"ISO8601时间"或null,"reminders":[],"cleanTask":"去时间后的文本"}

taskType规则：有要做的事+有时间点=deadline。只有明确含"跟进/查看进度/等回复/确认状态"且无结束时间才=followup。今晚/明早/下午/几点都是deadline。

=== intent="chat" 时返回： ===
{"intent":"chat","reply":"你的回答，200字内，简洁有帮助"}

当前时间：{current_time}
只返回JSON，不说别的。"""


def chat_reply(prompt: str) -> str | None:
    """简单 AI 回复，用于推送消息"""
    try:
        resp = requests.post(
            f"{LLM_CONFIG['base_url']}/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_CONFIG['api_key']}"},
            json={"model": LLM_CONFIG["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.9, "max_tokens": 150},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def analyze_with_llm(text: str) -> dict | None:
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S 北京时间")
    prompt = SYSTEM_PROMPT.replace("{current_time}", now_str)

    try:
        resp = requests.post(
            f"{LLM_CONFIG['base_url']}/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_CONFIG['api_key']}",
            },
            json={
                "model": LLM_CONFIG["model"],
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.0,
                "max_tokens": 400,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"LLM error: {e}")
    return None


def compute_absolute_time(date_str: str) -> str | None:
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    cn_num = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

    def cn2n(s):
        if s in cn_num: return cn_num[s]
        try: return int(s)
        except ValueError: return 0

    hour, minute = 23, 59
    tm = re.search(r"([一二三四五六七八九十两\d]{1,2})[点:：时](?:([一二三四五六七八九十两\d]{1,2})[分]?|(半))?", date_str)
    if tm:
        hour = cn2n(tm.group(1))
        minute = 30 if tm.group(3) == "半" else (cn2n(tm.group(2)) if tm.group(2) else 0)
        if any(w in date_str for w in ["下午", "傍晚"]):
            if hour != 12: hour += 12
        elif any(w in date_str for w in ["晚上", "明晚", "今晚"]):
            if hour < 12: hour += 12
        elif "中午" in date_str:
            hour = 12

    target = today
    if any(w in date_str for w in ["明天", "明早", "明晚"]):
        target = today + timedelta(days=1)
    elif "后天" in date_str:
        target = today + timedelta(days=2)
    elif "大后天" in date_str:
        target = today + timedelta(days=3)

    wd = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    wm = re.search(r"下下?周([一二三四五六日天])", date_str)
    if wm:
        delta = wd[wm.group(1)] - now.weekday()
        if "下下周" in date_str: delta += 14
        elif "下周" in date_str:
            if delta <= 0: delta += 7
        else:
            if delta <= 0: delta += 7
        target = today + timedelta(days=delta)

    md = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", date_str)
    if md:
        target = target.replace(month=int(md.group(1)), day=int(md.group(2)))
        if target < now: target = target.replace(year=target.year + 1)

    if target == today and not tm:
        return None

    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        return None
    return target.isoformat()


# ============= 四象限计算（服务端版本） =============

def is_important_category(cat: str) -> bool:
    return cat in ("作业限期", "会议安排", "信息提交")


def compute_quadrant(task: dict) -> str:
    """返回：救火区 / 投资区 / 干扰区 / 黑洞区"""
    try:
        important = is_important_category(task.get("category", "其他")) or task.get("priority", 1) >= 4
        urgent = False
        now = datetime.now(TZ)

        if task.get("taskType") == "followup" and task.get("nextCheckTime"):
            try:
                nc = safe_iso(task["nextCheckTime"])
                if nc <= now:
                    urgent = True
            except ValueError:
                pass
        elif task.get("dueTimestamp"):
            try:
                due = safe_iso(task["dueTimestamp"])
                diff_h = (due - now).total_seconds() / 3600
                if diff_h <= 4:
                    urgent = True
            except ValueError:
                pass

        if task.get("category") == "其他" and task.get("priority", 1) <= 1:
            urgent = False

        if important and urgent: return "救火区"
        elif important and not urgent: return "投资区"
        elif not important and urgent: return "干扰区"
        else: return "黑洞区"
    except Exception:
        return "黑洞区"


# ============= 微信消息处理 =============

load_all()  # 启动时加载已有数据

SITE_URL = os.environ.get("SITE_URL", "https://todo-bot-0ly4.onrender.com")
FOOTER = f"\n\n📊 查看任务面板：\n{SITE_URL}\n\n发送「帮助」查看所有命令"


def reply_with_footer(text: str) -> str:
    return text + FOOTER


def handle_text_message(from_user: str, to_user: str, content: str) -> str:
    msg = content.strip()
    tasks = get_user_tasks(from_user)
    now = datetime.now(TZ)

    # ===== 命令处理 =====
    # 帮助
    if msg in ('帮助', 'help', '?', '？', 'h'):
        return reply_with_footer(
            "📋 可用命令：\n\n"
            "▸ 直接发待办文本 → 自动添加\n"
            "▸ 列表 / list → 查看所有任务\n"
            "▸ 完成「任务名」→ 标记完成\n"
            "▸ 删除「任务名」→ 删除任务\n"
            "▸ 设置 → 查看当前设置\n"
            "▸ 帮助 → 显示此信息"
        )

    # 任务列表
    if msg in ('列表', 'list', 'ls', '查看', '任务', 'cx'):
        active = [t for t in tasks if not t.get('completed')]
        if not active:
            return reply_with_footer("🌸 暂无待办任务\n\n发送文本即可添加，如：\n明天下午3点开会")
        lines = [f"📋 共 {len(active)} 项待办：\n"]
        for i, t in enumerate(active[:15]):  # 最多显示15条
            label = t.get('label') or t.get('text', '')
            cat = t.get('category', '')
            quad = compute_quadrant(t)
            qe = {"救火区": "🔥", "投资区": "💎", "干扰区": "⚡", "黑洞区": "🕳"}
            due_str = ''
            if t.get('taskType') == 'followup' and t.get('nextCheckTime'):
                nc = safe_iso(t['nextCheckTime']).astimezone(TZ)
                due_str = f" | 检查 {nc.strftime('%m/%d %H:%M')}"
            elif t.get('dueTimestamp'):
                d = safe_iso(t['dueTimestamp']).astimezone(TZ)
                due_str = f" | {d.strftime('%m/%d %H:%M')}"
            lines.append(f"{i+1}. {qe.get(quad,'')} {label}  [{cat}]{due_str}")
        if len(active) > 15:
            lines.append(f"\n... 还有 {len(active)-15} 项，打开网页查看全部")
        return reply_with_footer('\n'.join(lines))

    # 完成任务（模糊匹配 label）
    for prefix in ('完成', 'done', 'finish', 'ok'):
        if msg.startswith(prefix):
            keyword = msg[len(prefix):].strip().replace('"', '').replace('"', '')
            if not keyword:
                return reply_with_footer("请指定要完成的任务名，如：完成 开会")
            found = None
            for t in tasks:
                lbl = t.get('label', '') or t.get('text', '')
                if keyword in lbl and not t.get('completed'):
                    found = t; break
            if found:
                found['completed'] = True
                found['completedAt'] = now.isoformat()
                save_all()
                return reply_with_footer(f"✅ 已完成：{found.get('label') or found.get('text')}")
            return reply_with_footer(f"未找到匹配的未完成任务：「{keyword}」")

    # 删除任务
    for prefix in ('删除', 'del', 'remove', 'rm'):
        if msg.startswith(prefix):
            keyword = msg[len(prefix):].strip().replace('"', '').replace('"', '')
            if not keyword:
                return reply_with_footer("请指定要删除的任务名，如：删除 取快递")
            found = None
            for t in tasks:
                lbl = t.get('label', '') or t.get('text', '')
                if keyword in lbl:
                    found = t; break
            if found:
                tasks.remove(found)
                save_all()
                return reply_with_footer(f"🗑 已删除：{found.get('label') or found.get('text')}")
            return reply_with_footer(f"未找到匹配的任务：「{keyword}」")

    # 查 OpenID
    if msg in ('whoami', 'id'):
        return reply_with_footer(f"你的 OpenID：{from_user}")

    # 设置
    if msg in ('设置', 'settings', 'config'):
        return reply_with_footer(
            "⚙ 当前提醒设置：\n"
            f"📚 学业：提前 {DEFAULT_REMINDERS['作业限期']} 分钟\n"
            f"💼 工作：提前 {DEFAULT_REMINDERS['会议安排']} 分钟\n"
            f"🏠 生活：提前 {DEFAULT_REMINDERS['生活琐事']} 分钟\n"
            f"🎮 休闲：无提醒\n"
            f"🔄 跟进间隔：每5天\n\n"
            "📅 日历订阅链接（iOS设置→日历→账户→添加订阅）：\n"
            f"{SITE_URL}/api/tasks.ics"
        )

    # 报告
    if msg in ('周报', 'weekly'):
        record_daily_stats()
        report = generate_report("weekly")
        return reply_with_footer(report)
    if msg in ('月报', 'monthly'):
        record_daily_stats()
        report = generate_report("monthly")
        return reply_with_footer(report)
    if msg in ('报告', 'report'):
        return reply_with_footer("发送「周报」或「月报」生成分析报告\n\n📅 订阅日历：\n" + SITE_URL + "/api/tasks.ics")

    # ===== AI 统一处理（意图识别） =====
    result = analyze_with_llm(msg)

    if result is None:
        return reply_with_footer(
            "抱歉，AI 暂时无法处理。\n\n"
            "可以试试：\n"
            "• 明天下午3点开会\n"
            "• 下周五提交ipa申请\n"
            "• 问 Python怎么入门\n"
            "• 帮我看看怎么学英语\n\n"
            "发送「帮助」查看所有命令"
        )

    # 闲聊意图 → 直接回复
    if result.get("intent") == "chat":
        return reply_with_footer(result.get("reply", "抱歉，我没理解你的意思"))

    task_type = result.get("taskType", "deadline")
    category = result.get("category", "其他")
    reminders = DEFAULT_REMINDERS.get(category, [30])

    # 计算绝对时间
    due_ts = None
    if result.get("hasDateTime") and result.get("dateTime"):
        due_ts = result.get("isoTime") or compute_absolute_time(result["dateTime"])

    now = datetime.now(TZ)
    now_ms = int(time.time() * 1000)

    task = {
        "id": f"{now_ms}-{hashlib.md5(content.encode()).hexdigest()[:6]}",
        "taskType": task_type,
        "label": result.get("label") or result.get("cleanTask") or content,
        "category": category,
        "priority": result.get("priority", 1),
        "notes": result.get("notes", content),
        "reminders": reminders,
        "text": result.get("cleanTask") or content,
        "completed": False,
        "createdAt": now_ms,
        "dueDate": result.get("dateTime") if result.get("hasDateTime") else None,
        "dueTimestamp": due_ts,
        "checkInterval": 7200 if task_type == "followup" else None,
        "nextCheckTime": (now + timedelta(minutes=7200)).isoformat() if task_type == "followup" else None,
        "lastCheckTime": None,
        "checkHistory": [],
        "userImportant": None,
        "source": "wechat",
    }

    tasks = get_user_tasks(from_user)
    tasks.append(task)
    save_all()

    # 计算象限
    quad = compute_quadrant(task)
    quad_emoji = {"救火区": "🔥", "投资区": "💎", "干扰区": "⚡", "黑洞区": "🕳"}

    # 构造回复
    cat_emoji = {"会议安排": "📅", "作业限期": "📚", "信息提交": "📤", "生活琐事": "🏠", "其他": "📌"}
    priority_bar = "★" * task["priority"] + "☆" * (5 - task["priority"])

    reply = f"✅ 已添加{'跟进' if task_type == 'followup' else '任务'}\n\n"
    reply += f"🏷 {task['label']}\n"
    reply += f"{cat_emoji.get(category, '📌')} {category}  {priority_bar}\n"
    reply += f"{quad_emoji.get(quad, '')} {quad}\n"

    if task_type == "followup":
        nc = safe_iso(task["nextCheckTime"]).astimezone(TZ)
        reply += f"🔄 下次检查：{nc.strftime('%m月%d日 %H:%M')}\n"
    elif task.get("dueTimestamp"):
        due_dt = safe_iso(task["dueTimestamp"]).astimezone(TZ)
        reply += f"⏰ {due_dt.strftime('%m月%d日 %H:%M')}"
        diff = due_dt - now
        hours_left = diff.total_seconds() / 3600
        if hours_left <= 0:
            reply += " · 已过期"
        elif hours_left < 24:
            reply += f" · 剩余{int(hours_left)}小时"
        reply += "\n"

    if task["notes"] != task["label"]:
        reply += f"📝 {task['notes']}\n"

    if reminders:
        remind_texts = []
        for r in reminders:
            if r >= 10080: remind_texts.append(f"提前{r // 10080}周")
            elif r >= 1440: remind_texts.append(f"提前{r // 1440}天")
            elif r >= 60: remind_texts.append(f"提前{r // 60}小时")
            else: remind_texts.append(f"提前{r}分钟")
        reply += f"🔔 {'、'.join(remind_texts)}提醒\n"

    return reply_with_footer(reply)


# ============= 微信主动推送 & 提醒检查 =============

WX_APP_ID = os.environ.get("WX_APP_ID", "")
WX_APP_SECRET = os.environ.get("WX_APP_SECRET", "")
_wx_token: dict = {"token": "", "expires": 0}


def get_wx_access_token() -> str:
    """获取微信 access_token，缓存到过期"""
    now = time.time()
    if _wx_token["token"] and _wx_token["expires"] > now + 300:
        return _wx_token["token"]
    try:
        r = requests.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": WX_APP_ID, "secret": WX_APP_SECRET},
            timeout=10,
        )
        data = r.json()
        _wx_token["token"] = data.get("access_token", "")
        _wx_token["expires"] = now + data.get("expires_in", 7200)
        return _wx_token["token"]
    except Exception as e:
        print(f"WX token error: {e}")
        return ""


def send_wx_message(openid: str, text: str) -> bool:
    """主动发送客服消息给用户"""
    token = get_wx_access_token()
    if not token or not openid:
        return False
    try:
        r = requests.post(
            f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
            json={"touser": openid, "msgtype": "text", "text": {"content": text}},
            timeout=10,
        )
        return r.json().get("errcode") == 0
    except Exception as e:
        print(f"Send fail: {e}")
        return False


def check_and_send_reminders() -> dict:
    """检查所有任务，对触发提醒的发送消息，返回发送统计"""
    load_all()
    now = datetime.now(TZ)
    sent_count = 0

    for openid, tasks in _all_tasks.items():
        for t in tasks:
            if t.get("completed"):
                continue

            # 跳过没有设置提醒的
            reminders = t.get("reminders", [])
            if not reminders and t.get("taskType") != "followup":
                continue

            # 已发送过的提醒不再重复
            sent = set(t.get("_reminded", []))

            # 跟进任务：检查时间到了就提醒
            if t.get("taskType") == "followup" and t.get("nextCheckTime"):
                nc = safe_iso(t["nextCheckTime"])
                if nc <= now and "check" not in sent:
                    label = t.get("label") or t.get("text", "")
                    send_wx_message(openid, f"🔍 跟进提醒\n\n「{label}」到检查时间了\n请在方便时处理并回复「完成 {label}」")
                    sent.add("check")
                    t["_reminded"] = list(sent)
                    sent_count += 1
                continue

            # DDL 任务：按提醒时间检查
            if not t.get("dueTimestamp"):
                continue

            due = safe_iso(t["dueTimestamp"])
            for r in reminders:
                remind_time = due - timedelta(minutes=r)
                if remind_time <= now < remind_time + timedelta(minutes=30):
                    if str(r) not in sent:
                        label = t.get("label") or t.get("text", "")
                        when = f"{r // 10080}周前" if r >= 10080 else f"{r // 1440}天前" if r >= 1440 else f"{r // 60}小时前" if r >= 60 else f"{r}分钟前"
                        send_wx_message(openid, f"⏰ 日程提醒\n\n「{label}」{when}到期\n截止时间：{due.strftime('%m月%d日 %H:%M')}")
                        sent.add(str(r))
                        t["_reminded"] = list(sent)
                        sent_count += 1

    if sent_count > 0:
        save_all()
    return {"reminders_sent": sent_count, "users_checked": len(_all_tasks)}


# ============= 企业微信加解密工具 =============

class WeworkCrypto:
    """企业微信消息加解密"""

    def __init__(self, token, aes_key, corp_id):
        self.token = token
        self.corp_id = corp_id.encode()
        self.key = base64.b64decode(aes_key + "=")

    def _pad(self, s: bytes, n: int = 32) -> bytes:
        pad = n - len(s) % n
        return s + bytes([pad] * pad)

    def _unpad(self, s: bytes) -> bytes:
        return s[:-s[-1]]

    def _encrypt(self, text: str) -> str:
        raw = (text.encode() + self.corp_id)
        import random
        raw = bytes([random.randint(0, 255) for _ in range(16)]) + \
            struct.pack("!I", len(text.encode())) + raw
        raw = self._pad(raw)
        iv = self.key[:16]
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()

    def _decrypt(self, encrypted: str) -> str:
        data = base64.b64decode(encrypted)
        iv = self.key[:16]
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        raw = decryptor.update(data) + decryptor.finalize()
        raw = self._unpad(raw)
        content_len = struct.unpack("!I", raw[16:20])[0]
        content = raw[20:20 + content_len].decode()
        return content

    def verify_signature(self, signature, timestamp, nonce, echostr):
        tmp = sorted([self.token, timestamp, nonce, echostr])
        return hashlib.sha1("".join(tmp).encode()).hexdigest() == signature

    def decrypt_msg(self, signature, timestamp, nonce, encrypted):
        if not self.verify_signature(signature, timestamp, nonce, encrypted):
            return None
        return self._decrypt(encrypted)


# 初始化企业微信加解密（需要 pycryptodome）
try:
    wework_crypto = WeworkCrypto(WEWORK_TOKEN, WEWORK_AES_KEY, WEWORK_CORP_ID) if HAS_CRYPTO else None
except Exception:
    wework_crypto = None

# 企业微信 access_token
_wework_token_cache = {"token": "", "expires": 0}


def get_wework_token() -> str:
    now = time.time()
    if _wework_token_cache["token"] and _wework_token_cache["expires"] > now + 300:
        return _wework_token_cache["token"]
    try:
        r = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": WEWORK_CORP_ID, "corpsecret": WEWORK_SECRET},
            timeout=10,
        )
        data = r.json()
        _wework_token_cache["token"] = data.get("access_token", "")
        _wework_token_cache["expires"] = now + data.get("expires_in", 7200)
        return _wework_token_cache["token"]
    except Exception as e:
        print(f"WeWork token error: {e}")
        return ""


def send_wework_message(userid: str, text: str) -> bool:
    """发送企业微信消息给指定用户"""
    token = get_wework_token()
    if not token:
        return False
    try:
        r = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json={
                "touser": userid,
                "msgtype": "text",
                "agentid": WEWORK_AGENT_ID,
                "text": {"content": text},
            },
            timeout=10,
        )
        return r.json().get("errcode") == 0
    except Exception as e:
        print(f"WeWork send fail: {e}")
        return False


# ============= Flask 路由 =============

@app.route("/wechat", methods=["GET", "POST"])
def wechat():
    if request.method == "GET":
        signature = request.args.get("signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")

        tmp = sorted([WECHAT_TOKEN, timestamp, nonce])
        if hashlib.sha1("".join(tmp).encode()).hexdigest() == signature:
            return echostr
        return "verification failed"

    try:
        raw = request.get_data()
        if not raw:
            return "ok"
        # 微信可能发 gzip 压缩数据
        if len(raw) >= 2 and raw[:2] == b'\x1f\x8b':
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        # 解码：微信发的是 UTF-8 XML
        xml_data = raw.decode('utf-8', errors='replace')
        root = ET.fromstring(xml_data)
        msg_type = root.findtext("MsgType", "")
        from_user = root.findtext("FromUserName", "")
        to_user = root.findtext("ToUserName", "")

        if msg_type == "text":
            content = root.findtext("Content", "")
            reply_text = handle_text_message(from_user, to_user, content)
        else:
            reply_text = "暂不支持此消息类型，请发送文字描述你的待办事项～"
    except Exception as e:
        reply_text = f"处理出错：{e}"
        from_user = ""
        to_user = ""

    reply_xml = (
        f"<xml>"
        f"<ToUserName><![CDATA[{from_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{to_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{reply_text}]]></Content>"
        f"</xml>"
    )
    return Response(reply_xml, content_type="application/xml; charset=utf-8")


@app.route("/wework", methods=["GET", "POST"])
def wework():
    """企业微信自建应用接收消息"""
    if not wework_crypto:
        return "crypto module not available", 500
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    if request.method == "GET":
        # 验证回调 URL
        echostr = request.args.get("echostr", "")
        decrypted = wework_crypto.decrypt_msg(msg_signature, timestamp, nonce, echostr)
        if decrypted:
            return decrypted
        return "verification failed"

    # POST: 接收消息（被动回复，不走 IP 白名单）
    try:
        raw = request.get_data()
        if len(raw) >= 2 and raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
        root = ET.fromstring(raw)
        encrypted = root.findtext("Encrypt", "")
        xml_data = wework_crypto.decrypt_msg(msg_signature, timestamp, nonce, encrypted)
        if not xml_data:
            return "decrypt failed"
        msg_root = ET.fromstring(xml_data)
        msg_type = msg_root.findtext("MsgType", "")
        from_user = msg_root.findtext("FromUserName", "")
        to_user = msg_root.findtext("ToUserName", "")

        if msg_type == "text":
            content = msg_root.findtext("Content", "")
            # 异步处理：先秒回确认，后台处理完再推结果
            def process_and_reply():
                try:
                    reply = handle_text_message(from_user, to_user, content)
                    send_wework_message(from_user, reply)
                except Exception as e:
                    print(f"WeWork bg error: {e}")
            threading.Thread(target=process_and_reply, daemon=True).start()
            # 立即回复空串 = 不显示任何内容，等待后台推送
            return ""
        return ""
    except Exception as e:
        print(f"WeWork error: {e}")
        return str(e)


@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    try:
        tasks = get_all_tasks_flat()
        # 按象限排序
        def sort_key(t):
            quad_order = {"救火区": 0, "投资区": 1, "干扰区": 2, "黑洞区": 3}
            q = compute_quadrant(t)
            return quad_order.get(q, 3) * 1000 - (t.get("priority", 1) * 100)
        tasks.sort(key=sort_key)
        return {"tasks": tasks}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


@app.route("/api/sync-all", methods=["POST"])
def api_sync_all():
    """网页端全量同步任务"""
    data = request.get_json(silent=True) or {}
    new_tasks = data.get("tasks", [])
    openid = data.get("openid", "web_user")
    # 保留 WeChat 来源的任务，替换网页端的
    existing = get_user_tasks(openid)
    wechat_ids = {t["id"] for t in existing if t.get("source") == "wechat"}
    merged = [t for t in existing if t["id"] in wechat_ids] + \
             [t for t in new_tasks if t["id"] not in wechat_ids]
    _all_tasks[openid] = merged
    save_all()
    return {"ok": True, "count": len(merged)}


@app.route("/api/sync-task", methods=["POST"])
def api_sync_task():
    """网页端同步任务到服务器"""
    data = request.get_json(silent=True) or {}
    task = data.get("task", {})
    openid = data.get("openid", "web_user")
    if not task.get("id"):
        return {"ok": False}, 400
    tasks = get_user_tasks(openid)
    # 去重：已存在同 id 就跳过
    if not any(t.get("id") == task["id"] for t in tasks):
        tasks.append(task)
        save_all()
    return {"ok": True}


@app.route("/api/add-task", methods=["GET", "POST"])
def api_add_task():
    """快捷指令专用：GET 参数直接传"""
    data = request.args if request.method == "GET" else (request.get_json(silent=True) or request.form or {})
    text = (data.get("text", "") or "").strip()
    openid = data.get("openid", "shortcuts_user")
    if not text:
        return {"ok": False, "error": "text is required"}, 400

    reply = handle_text_message(openid, "gh_shortcuts", text)
    return {"ok": True, "reply": reply}


@app.route("/my-ip")
def my_ip():
    """返回 Render 服务器 IP + 微信配置状态"""
    token_ok = bool(get_wx_access_token())
    return {
        "ip": request.remote_addr or "unknown",
        "wx_app_id_set": bool(WX_APP_ID),
        "wx_secret_set": bool(WX_APP_SECRET),
        "wx_token_ok": token_ok,
    }


@app.route("/scheduled-push", methods=["GET", "POST"])
def scheduled_push():
    """定时推送：13点推当日任务，22点推睡觉提醒"""
    load_all()
    now = datetime.now(TZ)
    hour = now.hour
    sent = 0

    for openid in _all_tasks:
        tasks = get_user_tasks(openid)
        # 下午1点：AI 写当日任务推送
        if hour == 13:
            active = [t for t in tasks if not t.get("completed")]
            if not active:
                continue
            task_list = "\n".join([
                f"- {t.get('label') or t.get('text','')} [{t.get('category','')}]"
                + (f" 截止{datetime.fromisoformat(t['dueTimestamp']).strftime('%H:%M')}" if t.get('dueTimestamp') and safe_iso(t['dueTimestamp']) else "")
                for t in active[:10]
            ])
            prompt = f"现在是{now.strftime('%H:%M')}。用温暖鼓励的语气写一条50字以内的今日任务提醒。任务：\n{task_list}\n\n不要列清单，自然地说。参考语气：'下午好！今天有3件事等着你——最重要的是下午两点的会。加油，一件一件来。'"
            msg = chat_reply(prompt) or f"📋 今日 {len(active)} 项待办，加油！"
            if send_wx_message(openid, msg):
                sent += 1

        # 晚上10点：AI 写睡觉提醒
        elif hour == 22:
            prompt = "用温柔关心的语气写一条30字以内的睡前提醒。针对ADHD用户，不要命令式。每次内容都不同。"
            msg = chat_reply(prompt) or "🌙 夜深了，该休息了。放下手机，明天的事交给明天的你。"
            if send_wx_message(openid, msg):
                sent += 1

    return {"sent_to": sent, "wx_errors": len(_all_tasks) - sent}


@app.route("/check-reminders", methods=["GET", "POST"])
def check_reminders():
    """定时提醒检查——由外部 cron 服务调用"""
    result = check_and_send_reminders()
    return result


@app.route("/api/settings", methods=["GET", "PUT"])
def api_settings():
    settings_file = "server_settings.json"
    if request.method == "GET":
        if os.path.exists(settings_file):
            with open(settings_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"education": {"reminders": [10080, 4320]}, "work": {"reminders": [2880, 120]},
                "life": {"reminders": [720]}, "play": {"reminders": []}, "followupInterval": 7200,
                "urgentHigh": 4, "urgentMid": 24}
    else:
        data = request.get_json()
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return {"ok": True}


@app.route("/api/tasks.ics")
def tasks_ics():
    """日历订阅：返回 iCalendar 格式，iOS/安卓可直接订阅"""
    load_all()
    now = datetime.now(TZ)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TodoBot//CN",
        "X-WR-CALNAME:任务四象限",
    ]
    for uid, tasks in _all_tasks.items():
        for t in tasks:
            if t.get("completed") or not t.get("dueTimestamp"):
                continue
            due = safe_iso(t["dueTimestamp"])
            label = t.get("label") or t.get("text", "")
            cat = t.get("category", "")
            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{t['id']}",
                f"DTSTART:{due.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND:{due.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{label} [{cat}]",
                f"DESCRIPTION:{t.get('notes', label)}",
                "END:VEVENT",
            ])
    lines.append("END:VCALENDAR")
    return Response("\n".join(lines), content_type="text/calendar; charset=utf-8")


# ============= 每日统计 & 报告 =============

STATS_FILE = "daily_stats.json"


def record_daily_stats():
    """记录当天任务快照"""
    load_all()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        stats = {}

    if today in stats:
        return  # 今天已记录

    all_tasks = get_all_tasks_flat()
    active = [t for t in all_tasks if not t.get("completed")]
    done = [t for t in all_tasks if t.get("completed")]
    cats = {}
    quads = {}
    for t in active:
        c = t.get("category", "其他")
        cats[c] = cats.get(c, 0) + 1
        q = compute_quadrant(t)
        quads[q] = quads.get(q, 0) + 1

    stats[today] = {
        "total_active": len(active),
        "total_done": len(done),
        "by_category": cats,
        "by_quadrant": quads,
        "users": len(_all_tasks),
    }
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def get_period_stats(days: int) -> dict:
    """获取最近 N 天的统计数据"""
    record_daily_stats()
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        stats = {}

    cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = {k: v for k, v in stats.items() if k >= cutoff}

    load_all()
    all_tasks = get_all_tasks_flat()
    active = [t for t in all_tasks if not t.get("completed")]
    done_recent = [t for t in all_tasks if t.get("completed") and t.get("completedAt", "") >= cutoff]

    return {
        "days": len(recent),
        "daily_snapshots": recent,
        "currently_active": len(active),
        "completed_in_period": len(done_recent),
        "active_by_quadrant": {q: len([t for t in active if compute_quadrant(t) == q]) for q in ["救火区", "投资区", "干扰区", "黑洞区"]},
    }


def generate_report(period: str) -> str:
    """生成周报/月报"""
    days = 7 if period == "weekly" else 30
    label = "周报" if period == "weekly" else "月报"
    stats = get_period_stats(days)

    prompt = f"""你是一个时间管理教练。根据用户过去{days}天的任务数据，生成一份简洁的{label}。

数据：
- 当前活跃任务：{stats['currently_active']} 项
- {label}内完成：{stats['completed_in_period']} 项
- 各象限分布：{stats['active_by_quadrant']}

请用以下格式回复（控制在400字内）：

📊 {label}总结
✅ 完成情况：（一句话评价）
🔍 问题发现：（1-2个关键问题）
💡 建议：（1-2条可执行的改进建议）
📈 趋势：（简短）

语气友善鼓励，面向ADHD用户。"""

    try:
        resp = requests.post(
            f"{LLM_CONFIG['base_url']}/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_CONFIG['api_key']}"},
            json={"model": LLM_CONFIG["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 500},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Report error: {e}")
        return f"📊 {label}\n\n当前活跃：{stats['currently_active']} 项\n本周完成：{stats['completed_in_period']} 项\n继续加油！"


@app.route("/api/report", methods=["GET"])
def api_report():
    period = request.args.get("period", "weekly")
    if period not in ("weekly", "monthly"):
        period = "weekly"
    report = generate_report(period)
    return {"period": period, "report": report, "stats": get_period_stats(7 if period == "weekly" else 30)}


@app.route("/")
def index():
    return send_from_directory(".", "vibecoding.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"启动日程助手后端... 端口: {port}")
    app.run(host="0.0.0.0", port=port)
