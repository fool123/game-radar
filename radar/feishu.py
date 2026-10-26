"""Server-side Feishu delivery; no retries or callback receiver.

Transport seam: callable(method, url, payload, headers, timeout) -> JSON dict.
Transport errors must raise, never return an HTTP status as a successful JSON body.
The caller owns durable delivery claims and idempotency across invocations.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit


HOST = "open.feishu.cn"
BASE = "https://" + HOST + "/open-apis/"


def _text(value, limit=300):
    return str(value or "")[:limit]


def _link(value):
    try:
        parsed = urlsplit(str(value))
        if (parsed.scheme in {"http", "https"} and parsed.hostname and
                not parsed.username and not parsed.password and
                not any(ord(c) < 33 for c in str(value)) and len(str(value)) <= 1500):
            return str(value)
    except ValueError:
        pass
    return ""


def build_card(report, report_url=''):
    """Return classic Feishu interactive card JSON (not the message envelope).

    Source-derived strings use plain_text, preventing Markdown/mention injection.
    Preview is bounded for the custom bot's 20 KB envelope limit.
    """
    elements = []

    def add(text):
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": text}})

    recommendations = report.get("recommendations") or []
    all_observations = report.get("observations") or []
    observations = [p for p in all_observations if p.get('discovery')!='seed']
    references = len(all_observations)-len(observations)
    add("已评估推荐：%d · 待研究观察：%d" % (len(recommendations), len(observations)))
    if references:
        add("参考库 %d 个仅在工作台查阅，不充当每日新发现。" % references)
    if report.get("summary"):
        add(_text(report["summary"], 500))
    if not recommendations:
        add("暂无已评估推荐。观察候选不代表已完成研究或获得推荐。")
    for title, projects in (("已评估推荐", recommendations), ("待研究观察", observations)):
        for project in projects[:4]:
            add(title + "｜" + _text(project.get("title"), 90) + "\n" +
                _text(project.get("summary"), 180) + "\n阶段：" + _text(project.get("stage", "unknown"), 25) +
                " · 事件日期：" + _text(project.get("event_at") or "未知", 40))
        if len(projects) > 4:
            add("其余 %d 个%s请在工作台查看。" % (len(projects) - 4, title))
    sources = report.get("sources") or []
    if not sources:
        add("来源状态：暂无采集记录。")
    for source in sources[:10]:
        add("来源：" + _text(source.get("name"), 50) + "｜" + _text(source.get("status", "unknown"), 20) +
            "\n" + _text(source.get("message"), 100))
    if len(sources) > 10:
        add("更多来源状态请在工作台查看。")
    link = _link(report_url)
    if link:
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {
            "tag": "plain_text", "content": "打开日报与反馈"}, "type": "primary", "url": link}]})
    add("报告 " + _text(report.get("id"), 60) + " · 规则 " + _text(report.get("rule_version"), 30) +
        "\n卡片仅提供报告入口；反馈在工作台保存。")
    return {"config": {"wide_screen_mode": True}, "header": {"template": "blue", "title": {
        "tag": "plain_text", "content": "游戏雷达 · " + _text(report.get("date"), 30)}}, "elements": elements}


def _valid_destination(url, webhook=False):
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname != HOST or p.port not in (None, 443):
            return False
        if p.username or p.password or p.fragment or any(ord(c) < 33 for c in url):
            return False
        if webhook:
            return bool(re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", p.path)) and not p.query
        return url in {BASE + "auth/v3/tenant_access_token/internal", BASE + "im/v1/messages?receive_id_type=chat_id"}
    except (ValueError, TypeError):
        return False


def _https_transport(method, url, payload, headers, timeout):
    """Connect only to a validated public Feishu IP with TLS hostname checks.

    Bypass environment proxies; do not follow redirects (secrets cannot migrate).
    DNS is resolved once; the checked IP is the socket's actual destination.
    """
    if not _valid_destination(url, "/bot/v2/hook/" in url):
        raise ValueError("invalid destination")
    addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise OSError("invalid DNS destination")
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(HOST, timeout=timeout, context=context)
    raw = socket.create_connection((addresses[0][4][0], 443), timeout=timeout)
    try:
        connection.sock = context.wrap_socket(raw, server_hostname=HOST)
        p = urlsplit(url)
        connection.request(method, p.path + ("?" + p.query if p.query else ""),
                           json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers)
        response = connection.getresponse()
        data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024 or not 200 <= response.status < 300:
            raise OSError("unconfirmed platform response")
        decoded = json.loads(data.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("invalid platform response")
        return decoded
    finally:
        connection.close()
        raw.close()


def _result(response, legacy=False):
    if not isinstance(response, dict):
        return "unknown"
    code = response.get("code", response.get("StatusCode") if legacy else None)
    # bool is not a platform integer return code, even though bool subclasses int.
    if type(code) is not int:
        return "unknown"
    return "sent" if code == 0 else "failed"


def send_report(report, config, transport=None):
    """Send exactly once; return unconfigured/failed/unknown/sent, no secrets.

    Configuration is server-side only. Unknown includes timeout, malformed JSON,
    and ambiguous HTTP errors; the caller must not automatically retry it.
    """
    mode = config.get("mode", "webhook")
    required = ("webhook_url",) if mode == "webhook" else ("app_id", "app_secret", "chat_id")
    if mode not in {"webhook", "app"}:
        return {"status": "failed", "error": "飞书模式无效。"}
    if any(not isinstance(config.get(key), str) or not config[key].strip() for key in required):
        return {"status": "unconfigured"}
    if mode == "webhook" and not _valid_destination(config["webhook_url"], webhook=True):
        return {"status": "failed", "error": "飞书 Webhook 地址无效。"}
    request = transport or _https_transport
    headers = {"Content-Type": "application/json; charset=utf-8"}
    card = build_card(report, config.get("report_url", ""))
    try:
        if mode == "webhook":
            payload = {"msg_type": "interactive", "card": card}
            if config.get("webhook_secret"):
                timestamp = str(int(time.time()))
                key = (timestamp + "\n" + config["webhook_secret"]).encode("utf-8")
                payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 20000:
                return {"status": "failed", "error": "飞书卡片超过大小限制。"}
            response = request("POST", config["webhook_url"], payload, headers, 10.0)
            status = _result(response, legacy=True)
        else:
            auth = request("POST", BASE + "auth/v3/tenant_access_token/internal",
                           {"app_id": config["app_id"], "app_secret": config["app_secret"]}, headers, 10.0)
            if _result(auth) != "sent" or not isinstance(auth.get("tenant_access_token"), str) or not auth["tenant_access_token"]:
                return {"status": "failed", "error": "飞书应用授权失败，消息未发送。"}
            headers = dict(headers, Authorization="Bearer " + auth["tenant_access_token"])
            identity = str(report.get("id", "")) + "\n" + config["chat_id"]
            payload = {"receive_id": config["chat_id"], "msg_type": "interactive",
                       "content": json.dumps(card, ensure_ascii=False),
                       "uuid": hashlib.sha256(identity.encode()).hexdigest()[:40]}
            response = request("POST", BASE + "im/v1/messages?receive_id_type=chat_id", payload, headers, 10.0)
            status = _result(response)
            if status == "sent":
                message_id = (response.get("data") or {}).get("message_id")
                if isinstance(message_id, str) and message_id:
                    return {"status": "sent", "message_id": message_id}
        if status == "sent":
            return {"status": "sent"}
        return {"status": status, "error": "飞书平台拒绝请求。" if status == "failed" else "发送结果未知，请人工核实，勿自动重试。"}
    except Exception:
        # Exception strings and remote messages may contain webhook URLs/tokens.
        return {"status": "unknown", "error": "发送结果未知，请人工核实，勿自动重试。"}


_LABELS = (
    ("content", "可能提及内容量不足", r"内容(?:太|过)?少|内容不足|流程太短|lack(?:ing)? (?:of )?content|too short"),
    ("repetition", "可能提及重复体验", r"重复|枯燥|repetitive|grind(?:y|ing)?"),
    ("core", "可能提及核心体验问题", r"不好玩|无聊|玩法差|boring|bad gameplay"),
    ("performance", "可能提及性能或稳定性问题", r"掉帧|卡顿|闪退|崩溃|优化差|crash(?:es|ing)?|stutter(?:ing)?|low fps"),
    ("price", "可能提及价格问题", r"太贵|价格高|贵|expensive|overpriced"),
)


def review_labels(text):
    """Keyword hints only, multilabel and editable; not model analysis or fact.

    Local negation catches simple Chinese/English constructions. Sarcasm, scope,
    quotations and nuanced sentiment require human review.
    """
    text = str(text or "").lower()
    labels = []
    for label, reason, pattern in _LABELS:
        for match in re.finditer(pattern, text):
            prefix = re.split(r"[，。！？,.;!?\n]", text[:match.start()])[-1][-20:]
            if re.search(r"(?:不是|不|没|没有|并非|并不|不再)(?:会|觉得|感到|出现|任何|明显|严重|很|太|那么)?$|\b(?:not|no|never|without)\s+(?:any\s+|at all\s+)?$", prefix):
                continue
            labels.append({"label": label, "reason": reason, "tentative": True})
            break
    return labels
