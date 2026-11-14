"""api.py: unexpected answers become ApiError, no secret lands in a message, no redirects with credentials."""

from __future__ import annotations

import io

import pytest
import requests

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


@pytest.mark.parametrize(
    "error_id",
    [
        {"auth_key": "TEST_SECRET"},
        ["TEST_SECRET"],
        "TEST_SECRET with spaces",
        "TEST_SECRET" + "x" * 40,
    ],
)
def test_pair_errors_never_echo_a_complex_error_id(post, error_id):
    """Only a short plain code is quoted; anything else could carry a credential into the log."""
    post(FakeResponse(body={"error_id": error_id}))
    with pytest.raises(api.ApiError) as exc:
        api.tv_pair_request("tv")
    assert "TEST_SECRET" not in str(exc.value) and "error_id unexpected" in str(exc.value)
    post(FakeResponse(body={"error_id": error_id}))
    with pytest.raises(api.ApiError) as exc:
        api.tv_pair_grant("tv", PAIRING, "1234")
    assert "TEST_SECRET" not in str(exc.value) and "error_id unexpected" in str(exc.value)


def test_pair_error_without_error_id_says_missing(post):
    post(FakeResponse(body={}))
    with pytest.raises(api.ApiError, match="error_id missing$"):
        api.tv_pair_request("tv")


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


# ---- malformed digest challenge -----------------------------------------------------------------
class _ChallengeAdapter(requests.adapters.BaseAdapter):
    """Local transport: every request gets this answer, or raises `fail`; nothing leaves the process."""

    def __init__(self, challenge: str = "", fail: Exception | None = None, then=None):
        super().__init__()
        # then: what the second request (the digest retry) gets: an exception or a body for HTTP 200.
        self.challenge, self.fail, self.then, self.sent = challenge, fail, then, 0
        self.authorization: list[str | None] = []

    def send(self, request, **kwargs):
        self.sent += 1
        self.authorization.append(request.headers.get("Authorization"))
        if self.fail is not None:
            raise self.fail
        r = requests.Response()
        r.url, r.request, r.raw, r.connection = request.url, request, io.BytesIO(b""), self
        if self.sent > 1 and self.then is not None:
            if isinstance(self.then, Exception):
                raise self.then
            r.status_code, r.reason = 200, "OK"
            r.headers = requests.structures.CaseInsensitiveDict({"Content-Type": "application/json"})
            r._content = self.then
            return r
        r.status_code, r.reason = 401, "Unauthorized"
        r.headers = requests.structures.CaseInsensitiveDict({"WWW-Authenticate": self.challenge})
        return r

    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    def install(**kwargs):
        adapter = _ChallengeAdapter(**kwargs)
        monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)
        return adapter

    return install


@pytest.mark.parametrize("challenge", ['Digest realm="tv"', 'Digest nonce="n"', "Digest"])
def test_malformed_digest_challenge_is_api_error(transport, challenge):
    adapter = transport(challenge=challenge)
    with pytest.raises(api.ApiError) as exc:
        api.tv_check_auth("tv.invalid", "dev", "s3cr3t-key")
    assert not isinstance(exc.value, api.TvAuthError) and "s3cr3t" not in str(exc.value)
    with pytest.raises(api.ApiError) as exc:
        api.tv_pair_grant("tv.invalid", PAIRING, "1234")
    assert "s3cr3t" not in str(exc.value)
    assert adapter.sent == 2  # one request per call; the malformed challenge sends no retry


def test_valid_digest_challenge_answers_with_the_retry(transport):
    adapter = transport(challenge='Digest realm="tv", nonce="n1", qop="auth"', then=b'{"powerstate": "On"}')
    api.tv_check_auth("tv.invalid", "dev", "key")
    assert adapter.sent == 2  # challenge, then the authorized retry
    assert adapter.authorization[0] is None and adapter.authorization[1].startswith("Digest ")
    assert 'username="dev"' in adapter.authorization[1]


def test_errors_outside_the_challenge_stay_visible(transport):
    """A failure while sending the authorized retry is no "unusable challenge"."""
    transport(challenge='Digest realm="tv", nonce="n1", qop="auth"', then=TypeError("boom"))
    with pytest.raises(TypeError):
        api.tv_check_auth("tv.invalid", "dev", "key")


def test_network_errors_stay_request_exceptions(transport):
    """InvalidURL is a ValueError too; it must not turn into an ApiError."""
    transport(fail=requests.exceptions.InvalidURL("bad"))
    with pytest.raises(requests.RequestException) as exc:
        api.tv_check_auth("tv.invalid", "dev", "s3cr3t-key")
    assert not isinstance(exc.value, api.ApiError)


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
        [{"success": {"username": "s3cr3t-user"}}],
        [{"success": {"username": 5, "clientkey": "s3cr3t-ck"}}],
        [{"success": {"username": "s3cr3t-user", "clientkey": ""}}],
        [{"error": {"type": {"username": "s3cr3t-user"}}}],
        [{"error": {"type": "s3cr3t-user and more"}}],
        [],
        [None],
        {"error": {"type": 101}},
    ],
)
def test_hue_register_unexpected_answer_is_api_error(post, body):
    post(FakeResponse(body=body))
    with pytest.raises(api.ApiError) as exc:
        api.hue_register("bridge")
    assert "s3cr3t" not in str(exc.value)


def test_hue_register_without_appid_is_api_error(post, get):
    post(FakeResponse(body=[{"success": {"username": "s3cr3t-user", "clientkey": "s3cr3t-ck"}}]))
    get(FakeResponse(status=302, headers={}))
    with pytest.raises(api.ApiError) as exc:
        api.hue_register("bridge")
    assert "s3cr3t" not in str(exc.value)


def test_hue_register_names_a_plain_error_type(post):
    post(FakeResponse(body=[{"error": {"type": 7, "description": "invalid value"}}]))
    with pytest.raises(api.ApiError, match="error type 7$"):
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
