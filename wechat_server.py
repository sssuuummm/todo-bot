"""
微信个人公众号 + 智能日程助手后端
Flask 服务，支持：微信消息 → LLM 分类解析 → 存储 → 回复 + 前端 API
"""
import json
import hashlib
import time
import re
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, request, Response, send_from_directory
import requests

app = Flask(__name__)

# ============= 配置 =============
WECHAT_TOKEN = os.environ.get("WECHAT_TOKEN", "your_wechat_token_here")

LLM_CONFIG = {
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-bf51236eafd94551916ba18b5854098b",
    "model": "deepseek-chat",
}

TASKS_FILE = "tasks.json"
TZ = timezone(timedelta(hours=8))

# 默认提醒规则（分钟）
DEFAULT_REMINDERS = {
    "作业限期": [10080, 4320],   # 7天, 3天
    "会议安排": [2880, 120],     # 2天, 2小时
    "信息提交": [2880, 120],     # 2天, 2小时
    "生活琐事": [720],           # 12小时
    "其他": [],
}

# ============= 任务存储 =============

def load_tasks():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


# ============= LLM 分析 =============

SYSTEM_PROMPT = """你是一个智能日程分析助手。分析用户输入的口语化待办文本，完成以下任务：

1. 判断任务类型（taskType）：
   - "deadline"：有明确截止时间/日期的任务
   - "followup"：需要持续跟进、检查进度、等待结果的无明确DDL任务（如查看审批、跟进邮件回复、确认进展、留意通知等）

2. 判断类别（category，严格从下列选一）：
   - 会议安排：会议、开会、碰头、讨论、视频、面试、约见、拜访、见面等
   - 作业限期：作业、论文、essay、ddl、deadline、考试、课程、答辩等
   - 信息提交：申请、提交材料、报名、注册、上传、审核、审批、盖章、填表、ipa等
   - 生活琐事：购物、买菜、缴费、快递、维修、家务、取件、挂号等
   - 其他：无法归入以上类别

3. 判断重要程度（priority 1-5），理解内容的内在价值：
   - 有长期价值的个人项目（如"开发网站"）→ 3-4分
   - 学业/工作硬性任务 → 4-5分
   - 普通生活事务 → 2-3分
   - 无长远价值的消遣 → 1-2分

4. 提取简洁主标签 label（5-12字），用户原始输入写入 notes。

5. 提取时间/日期信息。含精确时间则 hasDateTime=true，同时填 dateTime 和 isoTime。

6. reminders 留空数组即可。

当前时间：{current_time}

严格返回 JSON（不要包含其他任何文字）：
{"taskType":"deadline或followup","label":"主标签","category":"类别","priority":1-5,"notes":"用户原始输入","hasDateTime":true/false,"dateTime":"时间描述"或null,"isoTime":"ISO8601绝对时间如2026-05-21T15:00:00+08:00"或null,"reminders":[],"cleanTask":"去除时间后的文本"}"""


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
    important = is_important_category(task.get("category", "其他")) or task.get("priority", 1) >= 4
    urgent = False
    now = datetime.now(TZ)

    if task.get("taskType") == "followup" and task.get("nextCheckTime"):
        nc = datetime.fromisoformat(task["nextCheckTime"])
        if nc <= now:
            urgent = True
    elif task.get("dueTimestamp"):
        due = datetime.fromisoformat(task["dueTimestamp"])
        diff_h = (due - now).total_seconds() / 3600
        if diff_h <= 4:
            urgent = True

    if task.get("category") == "其他" and task.get("priority", 1) <= 1:
        urgent = False

    if important and urgent: return "救火区"
    elif important and not urgent: return "投资区"
    elif not important and urgent: return "干扰区"
    else: return "黑洞区"


# ============= 微信消息处理 =============

def handle_text_message(from_user: str, to_user: str, content: str) -> str:
    msg = content.strip()
    tasks = load_tasks()
    now = datetime.now(TZ)

    # ===== 命令处理 =====
    # 帮助
    if msg in ('帮助', 'help', '?', '？', 'h'):
        return (
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
            return "🌸 暂无待办任务\n\n发送文本即可添加，如：\n明天下午3点开会"
        lines = [f"📋 共 {len(active)} 项待办：\n"]
        for i, t in enumerate(active[:15]):  # 最多显示15条
            label = t.get('label') or t.get('text', '')
            cat = t.get('category', '')
            quad = compute_quadrant(t)
            qe = {"救火区": "🔥", "投资区": "💎", "干扰区": "⚡", "黑洞区": "🕳"}
            due_str = ''
            if t.get('taskType') == 'followup' and t.get('nextCheckTime'):
                nc = datetime.fromisoformat(t['nextCheckTime']).astimezone(TZ)
                due_str = f" | 检查 {nc.strftime('%m/%d %H:%M')}"
            elif t.get('dueTimestamp'):
                d = datetime.fromisoformat(t['dueTimestamp']).astimezone(TZ)
                due_str = f" | {d.strftime('%m/%d %H:%M')}"
            lines.append(f"{i+1}. {qe.get(quad,'')} {label}  [{cat}]{due_str}")
        if len(active) > 15:
            lines.append(f"\n... 还有 {len(active)-15} 项，打开网页查看全部")
        return '\n'.join(lines)

    # 完成任务（模糊匹配 label）
    for prefix in ('完成', 'done', 'finish', 'ok'):
        if msg.startswith(prefix):
            keyword = msg[len(prefix):].strip().replace('"', '').replace('"', '')
            if not keyword:
                return "请指定要完成的任务名，如：完成 开会"
            found = None
            for t in tasks:
                lbl = t.get('label', '') or t.get('text', '')
                if keyword in lbl and not t.get('completed'):
                    found = t; break
            if found:
                found['completed'] = True
                found['completedAt'] = now.isoformat()
                save_tasks(tasks)
                return f"✅ 已完成：{found.get('label') or found.get('text')}"
            return f"未找到匹配的未完成任务：「{keyword}」"

    # 删除任务
    for prefix in ('删除', 'del', 'remove', 'rm'):
        if msg.startswith(prefix):
            keyword = msg[len(prefix):].strip().replace('"', '').replace('"', '')
            if not keyword:
                return "请指定要删除的任务名，如：删除 取快递"
            found = None
            for t in tasks:
                lbl = t.get('label', '') or t.get('text', '')
                if keyword in lbl:
                    found = t; break
            if found:
                tasks.remove(found)
                save_tasks(tasks)
                return f"🗑 已删除：{found.get('label') or found.get('text')}"
            return f"未找到匹配的任务：「{keyword}」"

    # 设置
    if msg in ('设置', 'settings', 'config'):
        return (
            "⚙ 当前提醒设置：\n"
            f"📚 学业：提前 {DEFAULT_REMINDERS['作业限期']} 分钟\n"
            f"💼 工作：提前 {DEFAULT_REMINDERS['会议安排']} 分钟\n"
            f"🏠 生活：提前 {DEFAULT_REMINDERS['生活琐事']} 分钟\n"
            f"🎮 休闲：无提醒\n"
            f"🔄 跟进间隔：每5天\n\n"
            "📊 打开网页查看四象限视图：\n"
            f"https://todo-bot-0ly4.onrender.com"
        )

    # ===== AI 添加任务 =====
    result = analyze_with_llm(msg)

    if result is None:
        return (
            "抱歉，AI 暂时无法分析这条消息。\n\n"
            "试一试这样说：\n"
            "• 明天下午3点去301会议室开会\n"
            "• 下周五前提交ipa申请\n"
            "• 跟进一下审批进度\n\n"
            "发送「帮助」查看所有命令"
        )

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

    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)

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
        nc = datetime.fromisoformat(task["nextCheckTime"]).astimezone(TZ)
        reply += f"🔄 下次检查：{nc.strftime('%m月%d日 %H:%M')}\n"
    elif task.get("dueTimestamp"):
        due_dt = datetime.fromisoformat(task["dueTimestamp"]).astimezone(TZ)
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

    return reply


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

    xml_data = request.data.decode("utf-8")
    root = ET.fromstring(xml_data)
    msg_type = root.findtext("MsgType", "")
    from_user = root.findtext("FromUserName", "")
    to_user = root.findtext("ToUserName", "")

    if msg_type == "text":
        content = root.findtext("Content", "")
        reply_text = handle_text_message(from_user, to_user, content)
    else:
        reply_text = "暂不支持此消息类型，请发送文字描述你的待办事项～"

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


@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    tasks = load_tasks()
    # 按象限排序
    def sort_key(t):
        quad_order = {"救火区": 0, "投资区": 1, "干扰区": 2, "黑洞区": 3}
        q = compute_quadrant(t)
        return quad_order.get(q, 3) * 1000 - (t.get("priority", 1) * 100)
    tasks.sort(key=sort_key)
    return {"tasks": tasks}


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


@app.route("/")
def index():
    return send_from_directory(".", "vibecoding.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"启动日程助手后端... 端口: {port}")
    app.run(host="0.0.0.0", port=port)
