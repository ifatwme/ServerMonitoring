import io
import json
import urllib.error
import urllib.parse

import pytest

from servermon import telegram as tg


class FakeAPI:
    """Records Bot API calls; scripted responses per method (list = one per call)."""

    def __init__(self, responses=None):
        self.calls, self.responses, self.next_id = [], responses or {}, 100

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[1]
        params = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.calls.append((method, params))
        r = self.responses.get(method)
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        if r is None:
            self.next_id += 1
            r = {"ok": True, "result": {"message_id": self.next_id, "message_thread_id": self.next_id}}
        return io.BytesIO(json.dumps(r).encode())


def http_error(code, body):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


def bot(api):
    return tg.Bot("123:SECRET", opener=api, sleep=lambda s: None)


def test_send_uses_html_thread_and_silent_flags():
    api = FakeAPI()
    bot(api).send(-100, "<b>x</b>", thread_id=7, silent=True)
    m, p = api.calls[0]
    assert m == "sendMessage" and p["parse_mode"] == "HTML" and p["message_thread_id"] == "7"
    assert p["disable_notification"] == "True" and json.loads(p["link_preview_options"]) == {"is_disabled": True}
    api2 = FakeAPI()
    bot(api2).send(-100, "hi")
    assert "message_thread_id" not in api2.calls[0][1] and "disable_notification" not in api2.calls[0][1]


def test_rate_limit_is_retried_and_errors_never_leak_the_token():
    api = FakeAPI({"sendMessage": [http_error(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}),
                                   {"ok": True, "result": {"message_id": 1}}]})
    assert bot(api).send(-100, "x") == [1] and len(api.calls) == 2
    api = FakeAPI({"sendMessage": [http_error(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})]})
    with pytest.raises(tg.TelegramError) as e:
        bot(api).send(-100, "x")
    assert "chat not found" in str(e.value) and "SECRET" not in str(e.value)
    api = FakeAPI({"getMe": [OSError("down")] * 5})
    with pytest.raises(tg.TelegramError) as e:
        bot(api).call("getMe")
    assert "SECRET" not in str(e.value)


def test_split_keeps_messages_under_the_limit_and_pre_balanced():
    text = "head\n<pre>" + "\n".join(f"row {i:04d} " + "x" * 60 for i in range(200)) + "</pre>\ntail"
    parts = tg.split(text)
    assert len(parts) > 1 and all(len(p) <= tg.LIMIT for p in parts)
    for p in parts:
        assert p.count("<pre>") == p.count("</pre>")
    assert tg.split("short") == ["short"]


def test_ensure_topics_creates_only_missing_and_announces():
    api = FakeAPI()
    state = tg.ensure_topics(bot(api), -100, {"a": "A", "b": "B"}, {"topics": {"a": 5}})
    created = [p for m, p in api.calls if m == "createForumTopic"]
    assert [c["name"] for c in created] == ["B"] and state["topics"]["a"] == 5 and state["topics"]["b"] > 100
    intro = [p for m, p in api.calls if m == "sendMessage"]
    assert intro and intro[0]["message_thread_id"] == str(state["topics"]["b"])


def test_alertmanager_routing_and_template():
    yml, tmpl = tg.render_alertmanager(-1001234567890, {"web": 11, "api": 12}, "https://g.example/")
    assert "message_thread_id: 11" in yml and "message_thread_id: 12" in yml
    assert yml.count("disable_notifications: true") == 3          # general-silent + 2 project-silent
    assert 'project="web"' in yml and "receiver: project-web-silent" in yml
    assert "chat_id: -1001234567890" in yml and "bot_token_file: /etc/alertmanager/bot_token" in yml
    assert "SECRET" not in yml
    assert '| html' in tmpl and "https://g.example/d/sm-project?var-project=" in tmpl
    _, plain = tg.render_alertmanager(-1, {}, "")
    assert "href" not in plain


def test_write_alertmanager_null_receiver_without_settings(tmp_path):
    assert tg.write_alertmanager(None, None, "", am_dir=tmp_path) is False
    assert "receiver: none" in (tmp_path / "alertmanager.yml").read_text()
    assert not (tmp_path / "bot_token").exists()
    assert tg.write_alertmanager(-1, {"a": 3}, "123:SECRET", am_dir=tmp_path) is True
    assert (tmp_path / "bot_token").read_text() == "123:SECRET"
    assert (tmp_path / "bot_token").stat().st_mode & 0o777 == 0o640


def test_topics_created_by_hand_are_adopted_by_name():
    updates = [
        {"message": {"chat": {"id": -100}, "message_thread_id": 12, "forum_topic_created": {"name": "Papersaurus"}}},
        {"message": {"chat": {"id": -100}, "message_thread_id": 16, "forum_topic_created": {"name": "Deutsch Hub"}}},
        {"message": {"chat": {"id": -100}, "message_thread_id": 16, "forum_topic_edited": {"name": "🇩🇪 Deutsch Practice Hub"}}},
        {"message": {"chat": {"id": -999}, "message_thread_id": 30, "forum_topic_created": {"name": "EmoTrack"}}},
    ]
    api = FakeAPI()
    state = tg.ensure_topics(bot(api), -100, {"paper": "📚 Papersaurus", "de": "Deutsch Practice Hub",
                                              "emo": "🎭 EmoTrack"}, {}, updates)
    assert state["topics"]["paper"] == 12 and state["topics"]["de"] == 16
    assert state["topics"]["emo"] > 100                     # other chat ignored -> created
    assert [p["name"] for m, p in api.calls if m == "createForumTopic"] == ["🎭 EmoTrack"]
