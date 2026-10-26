import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from radar import feishu


REPORT = {"id": "r1", "date": "2026-10-24", "recommendations": [],
          "observations": [], "sources": [{"name": "Steam", "status": "failed", "message": "网络失败"}]}
WEBHOOK = {"mode": "webhook", "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test"}
APP = {"mode": "app", "app_id": "id", "app_secret": "secret", "chat_id": "oc_chat"}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, payload, headers, timeout):
        self.calls.append((method, url, payload, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FeishuTests(unittest.TestCase):
    def test_unconfigured_never_contacts_network(self):
        transport = FakeTransport()
        for config in ({}, {"mode": "app", "app_id": "x"}, {"mode": "webhook"}):
            self.assertEqual(feishu.send_report(REPORT, config, transport)["status"], "unconfigured")
        self.assertEqual(transport.calls, [])

    def test_empty_card_keeps_failure_and_does_not_invent_recommendation(self):
        card = feishu.build_card(REPORT)
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("暂无已评估推荐", rendered)
        self.assertIn("Steam", rendered)
        self.assertIn("failed", rendered)
        self.assertIn("网络失败", rendered)
        self.assertNotIn("action", rendered)

    def test_card_bounds_and_literal_untrusted_content(self):
        report = dict(REPORT, recommendations=[{"title": "<at id=all>" + "长" * 20000, "summary": "字" * 20000}] * 30)
        card = feishu.build_card(report, "javascript:alert(1)")
        self.assertLess(len(json.dumps(card, ensure_ascii=False).encode()), 19000)
        self.assertNotIn("javascript:", json.dumps(card))
        for element in card["elements"]:
            if element["tag"] == "div":
                self.assertEqual(element["text"]["tag"], "plain_text")

    def test_reference_library_does_not_fill_daily_watch_items(self):
        report=dict(REPORT,observations=[{'title':'历史校准案例','discovery':'seed'},{'title':'新待验证线索','discovery':'feed'}])
        rendered=json.dumps(feishu.build_card(report),ensure_ascii=False)
        self.assertNotIn('历史校准案例',rendered)
        self.assertIn('新待验证线索',rendered)
        self.assertIn('参考库 1 个',rendered)

    def test_webhook_checks_platform_code_and_signs_documented_formula(self):
        transport = FakeTransport({"code": 19021, "msg": "secret private url"})
        with patch("radar.feishu.time.time", return_value=1599360473):
            result = feishu.send_report(REPORT, dict(WEBHOOK, webhook_secret="demo"), transport)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", json.dumps(result))
        payload = transport.calls[0][2]
        expected = base64.b64encode(hmac.new(b"1599360473\ndemo", b"", hashlib.sha256).digest()).decode()
        self.assertEqual(payload["sign"], expected)
        self.assertEqual(payload["timestamp"], "1599360473")
        self.assertEqual(payload["msg_type"], "interactive")

    def test_legacy_webhook_success_and_contradiction(self):
        for response, expected in (({"StatusCode": 0}, "sent"), ({"code": 0}, "sent"),
                                   ({"code": 3, "StatusCode": 0}, "failed"), ({}, "unknown")):
            with self.subTest(response=response):
                self.assertEqual(feishu.send_report(REPORT, WEBHOOK, FakeTransport(response))["status"], expected)

    def test_timeout_is_unknown_and_not_retried(self):
        transport = FakeTransport(TimeoutError("private secret url"))
        result = feishu.send_report(REPORT, WEBHOOK, transport)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(transport.calls), 1)

    def test_app_auth_and_message_contract(self):
        transport = FakeTransport({"code": 0, "tenant_access_token": "token"},
                                  {"code": 0, "data": {"message_id": "om_1"}})
        result = feishu.send_report(REPORT, APP, transport)
        self.assertEqual(result, {"status": "sent", "message_id": "om_1"})
        auth, send = transport.calls
        self.assertTrue(auth[1].endswith("/auth/v3/tenant_access_token/internal"))
        self.assertEqual(auth[2], {"app_id": "id", "app_secret": "secret"})
        self.assertTrue(send[1].endswith("/messages?receive_id_type=chat_id"))
        self.assertEqual(send[2]["receive_id"], "oc_chat")
        self.assertEqual(send[3]["Authorization"], "Bearer token")
        self.assertEqual(json.loads(send[2]["content"]), feishu.build_card(REPORT))
        self.assertLessEqual(len(send[2]["uuid"]), 50)

    def test_auth_rejection_never_sends_message(self):
        transport = FakeTransport({"code": 99991661, "msg": "secret"})
        self.assertEqual(feishu.send_report(REPORT, APP, transport)["status"], "failed")
        self.assertEqual(len(transport.calls), 1)

    def test_http200_with_invalid_app_reply_is_unknown(self):
        transport = FakeTransport({"code": 0, "tenant_access_token": "token"}, {"data": {}})
        self.assertEqual(feishu.send_report(REPORT, APP, transport)["status"], "unknown")

    def test_app_platform_rejection_is_not_success(self):
        transport = FakeTransport({"code": 0, "tenant_access_token": "token"},
                                  {"code": 230001, "msg": "private error"})
        self.assertEqual(feishu.send_report(REPORT, APP, transport)["status"], "failed")

    def test_report_link_uses_navigation_without_callback(self):
        card = feishu.build_card(REPORT, "https://example.org/report/r1")
        action = [e for e in card["elements"] if e["tag"] == "action"][0]["actions"][0]
        self.assertEqual(action["url"], "https://example.org/report/r1")
        self.assertNotIn("value", action)

    def test_real_transport_blocks_private_dns_before_socket(self):
        with patch("radar.feishu.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), \
             patch("radar.feishu.socket.create_connection") as connect:
            result = feishu.send_report(REPORT, WEBHOOK)
        self.assertEqual(result["status"], "unknown")
        connect.assert_not_called()

    def test_webhook_destination_rejects_private_and_deceptive_urls(self):
        for url in ("http://open.feishu.cn/open-apis/bot/v2/hook/x", "https://127.0.0.1/x",
                    "https://open.feishu.cn.evil.test/open-apis/bot/v2/hook/x",
                    "https://user@open.feishu.cn/open-apis/bot/v2/hook/x",
                    "https://open.feishu.cn:444/open-apis/bot/v2/hook/x"):
            transport = FakeTransport()
            self.assertEqual(feishu.send_report(REPORT, dict(WEBHOOK, webhook_url=url), transport)["status"], "failed")
            self.assertEqual(transport.calls, [])

    def test_mixed_review_reasons_coexist_and_remain_tentative(self):
        labels = feishu.review_labels("内容太少，重复刷怪很无聊，核心玩法不好玩，还频繁掉帧，价格太贵。")
        self.assertEqual({item["label"] for item in labels}, {"content", "repetition", "core", "performance", "price"})
        self.assertTrue(all(item["tentative"] for item in labels))

    def test_obvious_negations_and_neutral_reviews_not_mislabeled(self):
        for text in ("内容不少，不重复，不无聊，没有掉帧，不贵。", "not boring, no crashes, not expensive", "画面很好", ""):
            self.assertEqual(feishu.review_labels(text), [], text)

    def test_negation_does_not_hide_separate_negative_reason(self):
        labels = feishu.review_labels("不是无聊，也没有任何卡顿，而是内容太少。")
        self.assertEqual([item["label"] for item in labels], ["content"])


if __name__ == "__main__":
    unittest.main()
