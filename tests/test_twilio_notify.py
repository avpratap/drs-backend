"""Twilio notification senders + SendGrid email: routing and request shape."""
import httpx
import pytest

from app.core.config import settings
from app.services import twilio_msg
from app.services.notifications import senders


class _Resp:
    def __init__(self, status=201, json_data=None, headers=None, text=""):
        self.status_code = status
        self._j = json_data or {}
        self.headers = headers or {"content-type": "application/json"}
        self.text = text

    def json(self):
        return self._j


class _Client:
    """Fake httpx.AsyncClient capturing the last POST; returns _Client.resp."""

    calls: list = []
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        _Client.calls.append({"url": url, **kw})
        return _Client.resp


@pytest.fixture
def twilio_on(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "tok_test")
    monkeypatch.setattr(settings, "TWILIO_SMS_FROM", "+15550001111")
    monkeypatch.setattr(settings, "TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    _Client.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_twilio_send_sms_builds_request(twilio_on):
    _Client.resp = _Resp(201, {"sid": "SM123"})
    ok, sid, err = await twilio_msg.send_sms("9876543210", "hello there")
    assert (ok, sid, err) == (True, "SM123", None)
    call = _Client.calls[-1]
    assert call["url"].endswith("/Accounts/AC_test/Messages.json")
    assert call["data"] == {"From": "+15550001111", "To": "+919876543210", "Body": "hello there"}
    assert call["auth"] == ("AC_test", "tok_test")


async def test_twilio_send_whatsapp_prefixes(twilio_on):
    _Client.resp = _Resp(201, {"sid": "WA1"})
    ok, sid, _ = await twilio_msg.send_whatsapp("9876543210", "hi")
    assert ok and sid == "WA1"
    d = _Client.calls[-1]["data"]
    assert d["From"] == "whatsapp:+14155238886"
    assert d["To"] == "whatsapp:+919876543210"


async def test_twilio_sms_error_status(twilio_on):
    _Client.resp = _Resp(400, {}, text="bad number")
    ok, sid, err = await twilio_msg.send_sms("9876543210", "x")
    assert ok is False and sid is None and "twilio 400" in err


async def test_notifications_sms_routes_to_twilio(twilio_on):
    _Client.resp = _Resp(201, {"sid": "SMnotify"})
    ok, sid, err = await senders.send_sms("9876543210", "order out for delivery", None)
    assert (ok, sid, err) == (True, "SMnotify", None)


async def test_notifications_whatsapp_routes_to_twilio(twilio_on):
    _Client.resp = _Resp(201, {"sid": "WAnotify"})
    ok, sid, _ = await senders.send_whatsapp("9876543210", "delivered", None)
    assert ok and sid == "WAnotify"


async def test_sms_channel_available_when_twilio_configured(twilio_on, monkeypatch):
    monkeypatch.setattr(settings, "ENV", "prod")
    assert senders.channel_unavailable("sms") is None  # Twilio can deliver


async def test_sms_channel_unavailable_in_prod_without_twilio(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "ENV", "prod")
    assert senders.channel_unavailable("sms") == "provider_unavailable"


async def test_send_email_via_sendgrid(monkeypatch):
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setattr(settings, "SENDGRID_FROM", "no-reply@drs.co.in")
    _Client.calls = []
    _Client.resp = _Resp(202, {}, headers={"X-Message-Id": "sg-1"})
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok, mid, err = await senders.send_email("buyer@example.com", "verify your email", {"subject": "Verify"})
    assert (ok, mid, err) == (True, "sg-1", None)
    call = _Client.calls[-1]
    assert call["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert call["headers"]["Authorization"] == "Bearer SG.testkey"
    assert call["json"]["from"]["email"] == "no-reply@drs.co.in"
    assert call["json"]["personalizations"][0]["to"][0]["email"] == "buyer@example.com"
    assert call["json"]["subject"] == "Verify"
