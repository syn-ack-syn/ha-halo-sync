"""api.py: unexpected answers become ApiError, no secret lands in a message, no redirects with credentials."""

from __future__ import annotations

import pytest

from halo_sync import api


class FakeResponse:
    def __init__(self, status: int = 200, body=None, headers: dict | None = None, raw: bool = False):
        self.status_code = status
        self._body = body
        self._raw = raw
        self.headers = headers or {}
        self.text = "" if raw else repr(body)

    def json(self):
        if self._raw:
            raise ValueError("no json")
        return self._body


class Recorder:
    """Stands in for requests.get/post: returns the queued answers and keeps the keyword arguments."""

    def __init__(self, *answers: FakeResponse):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.answers.pop(0)


@pytest.fixture
def post(monkeypatch):
    def install(*answers):
        rec = Recorder(*answers)
        monkeypatch.setattr(api.requests, "post", rec)
        return rec

    return install


@pytest.fixture
def get(monkeypatch):
    def install(*answers):
        rec = Recorder(*answers)
        monkeypatch.setattr(api.requests, "get", rec)
        return rec

    return install


PAIRING = {"id": "dev", "auth_key": "s3cr3t-key", "timestamp": 123}


# ---- TV pairing request -------------------------------------------------------------------------
def test_pair_request_ok(post):
    post(FakeResponse(body={"error_id": "SUCCESS", "auth_key": "abc", "timestamp": 42}))
    result = api.tv_pair_request("tv")
    assert result["auth_key"] == "abc" and result["timestamp"] == 42 and len(result["id"]) == 16


def test_pair_request_missing_field_names_only_the_field(post):
    post(FakeResponse(body={"error_id": "SUCCESS", "auth_key": "s3cr3t-key"}))
    with pytest.raises(api.ApiError) as exc:
        api.tv_pair_request("tv")
    assert "timestamp" in str(exc.value)
    assert "s3cr3t-key" not in str(exc.value)


def test_pair_request_failure_does_not_echo_the_answer(post):
    post(FakeResponse(body={"error_id": "CONCURRENT_PAIRING", "auth_key": "s3cr3t-key"}))
    with pytest.raises(api.ApiError) as exc:
        api.tv_pair_request("tv")
    assert "CONCURRENT_PAIRING" in str(exc.value)
    assert "s3cr3t-key" not in str(exc.value)


@pytest.mark.parametrize("answer", [FakeResponse(body=["x"]), FakeResponse(raw=True), FakeResponse(body=None)])
def test_pair_request_unexpected_answer_is_api_error(post, answer):
    post(answer)
    with pytest.raises(api.ApiError):
        api.tv_pair_request("tv")


# ---- TV pairing grant ---------------------------------------------------------------------------
def test_pair_grant_ok_without_redirects(post):
    rec = post(FakeResponse(body={"error_id": "SUCCESS"}))
    assert api.tv_pair_grant("tv", PAIRING, "1234") == ("dev", "s3cr3t-key")
    assert rec.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "answer",
    [
        FakeResponse(body=["SUCCESS"]),
        FakeResponse(body="SUCCESS"),
        FakeResponse(raw=True),
        FakeResponse(body={"error_id": "INVALID_PIN"}),
        FakeResponse(status=302, body={"error_id": "SUCCESS"}),
    ],
)
def test_pair_grant_unexpected_answer_is_api_error(post, answer):
    post(answer)
    with pytest.raises(api.ApiError) as exc:
        api.tv_pair_grant("tv", PAIRING, "1234")
    assert "s3cr3t-key" not in str(exc.value)


# ---- TV auth check ------------------------------------------------------------------------------
def test_tv_check_auth(get):
    rec = get(FakeResponse(status=200), FakeResponse(status=401), FakeResponse(status=301))
    api.tv_check_auth("tv", "u", "p")
    with pytest.raises(api.TvAuthError):
        api.tv_check_auth("tv", "u", "p")
    with pytest.raises(api.ApiError):
        api.tv_check_auth("tv", "u", "p")
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in rec.calls)


# ---- Hue registration ---------------------------------------------------------------------------
def test_hue_register_ok(post, get):
    post(FakeResponse(body=[{"success": {"username": "user", "clientkey": "ck"}}]))
    rec = get(FakeResponse(headers={"hue-application-id": "appid"}))
    assert api.hue_register("bridge") == {"username": "user", "clientkey": "ck", "appid": "appid"}
    assert rec.calls[0][1]["allow_redirects"] is False


def test_hue_register_link_button(post):
    post(FakeResponse(body=[{"error": {"type": 101, "description": "link button not pressed"}}]))
    assert api.hue_register("bridge") is None


@pytest.mark.parametrize(
    "body",
    [
        [{"error": None}],
        [{"error": "x"}],
        [{"error": {"type": 7}}],
        [{"success": None, "error": None}],
        [{"success": "x"}],
        [{"success": {"username": "user"}}],
        [{"success": {"username": 5, "clientkey": "ck"}}],
        [],
        [None],
        {"error": {"type": 101}},
    ],
)
def test_hue_register_unexpected_answer_is_api_error(post, body):
    post(FakeResponse(body=body))
    with pytest.raises(api.ApiError):
        api.hue_register("bridge")


def test_hue_register_without_appid_is_api_error(post, get):
    post(FakeResponse(body=[{"success": {"username": "user", "clientkey": "ck"}}]))
    get(FakeResponse(status=302, headers={}))
    with pytest.raises(api.ApiError):
        api.hue_register("bridge")


# ---- Hue entertainment areas --------------------------------------------------------------------
def test_hue_areas_ok_without_redirects(get):
    rec = get(FakeResponse(body={"data": [{"id": "a1", "metadata": {"name": "Living room"}}]}))
    assert api.hue_entertainment_areas("bridge", "key") == {"a1": "Living room"}
    assert rec.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("status", [401, 403])
def test_hue_areas_rejected_key_is_hue_auth_error(get, status):
    get(FakeResponse(status=status, body={"errors": [], "data": []}))
    with pytest.raises(api.HueAuthError):
        api.hue_entertainment_areas("bridge", "key")


@pytest.mark.parametrize(
    "answer",
    [
        FakeResponse(status=302, body={"data": []}),
        FakeResponse(status=500, body={"data": []}),
        FakeResponse(raw=True),
        FakeResponse(body=[]),
        FakeResponse(body={"data": None}),
        FakeResponse(body={"data": [None]}),
        FakeResponse(body={"data": [{"id": "a1"}]}),
        FakeResponse(body={"data": [{"id": "a1", "metadata": {"name": None}}]}),
    ],
)
def test_hue_areas_unexpected_answer_is_api_error(get, answer):
    get(answer)
    with pytest.raises(api.ApiError) as exc:
        api.hue_entertainment_areas("bridge", "key")
    assert not isinstance(exc.value, api.HueAuthError)
