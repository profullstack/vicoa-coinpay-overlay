import pytest

from cloud.billing import intent as it


def _mk(now=1_000_000.0):
    return it.new_intent(user_id="u1", plan_type="pro", billing_interval="monthly",
                         success_url="http://web.test/ok", cancel_url="http://web.test/no", ttl_seconds=60, now=now)


def test_round_trip():
    i = _mk()
    token = it.encode(i, "s3cret")
    assert it.decode(token, "s3cret", now=1_000_010) == i


def test_bad_signature_and_wrong_secret():
    token = it.encode(_mk(), "s3cret")
    with pytest.raises(it.IntentError):
        it.decode(token[:-1] + ("0" if token[-1] != "0" else "1"), "s3cret", now=1_000_010)
    with pytest.raises(it.IntentError):
        it.decode(token, "other", now=1_000_010)


def test_expired_and_malformed():
    token = it.encode(_mk(), "s3cret")
    with pytest.raises(it.IntentError, match="expired"):
        it.decode(token, "s3cret", now=1_000_061)
    with pytest.raises(it.IntentError):
        it.decode("nodot", "s3cret")
    with pytest.raises(it.IntentError):
        it.encode(_mk(), "")


def test_tampered_payload_rejected():
    token = it.encode(_mk(), "s3cret")
    payload, sig = token.split(".")
    other = it.encode(_mk(), "s3cret").split(".")[0]
    with pytest.raises(it.IntentError):
        it.decode(f"{other}.{sig}", "s3cret", now=1_000_010)
