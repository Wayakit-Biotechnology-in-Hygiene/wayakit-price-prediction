"""Review I1 (commit d9013ee): Bedrock errors must never leak an ARN or an account ID.

The Selenium scrapers log every exception with `exc_info=True`. Before the fix the agent raised
`RelevanceAgentFatalError(...) from error`, so the logged traceback included the chained botocore
`ClientError`, whose AWS message carries the role ARN and the account ID. Here the fake AWS errors
carry an obviously fake ARN (`arn:aws:iam::000000000000:role/fake`) and the tests require that it
never appears in: the exception chain (`__cause__`), the formatted traceback, a log line written
the way the scrapers write them, or the logged model IDs (application inference profile ARNs, F3).

Also covers the review nits: botocore network errors are transient (never fatal), and the model
IDs that end up in logs are never a full ARN.
"""
from __future__ import annotations

import logging
import random
import traceback

import pytest
from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError,
                                 EndpointConnectionError, HTTPClientError, ReadTimeoutError)

from tests.conftest import load_module
from tests.unit._fake_bedrock import FakeBedrock

pytestmark = pytest.mark.unit

ai = load_module("scraper/services/ai_service.py")
PRIMARY, FALLBACK = ai.DEFAULT_PRIMARY_MODEL, ai.DEFAULT_FALLBACK_MODEL
FAKE_ACCOUNT = "000000000000"
FAKE_ARN = f"arn:aws:iam::{FAKE_ACCOUNT}:role/fake-pi-role"
PROFILE_ARN = f"arn:aws:bedrock:eu-north-1:{FAKE_ACCOUNT}:application-inference-profile/fakeprofile01"


@pytest.fixture(autouse=True)
def _no_model_env(monkeypatch):
    monkeypatch.delenv(ai.PRIMARY_MODEL_ENV, raising=False)
    monkeypatch.delenv(ai.FALLBACK_MODEL_ENV, raising=False)


@pytest.fixture
def agent_logs(caplog):
    """The agent's logger (the file-less test log_config one) does not propagate: attach caplog."""
    ai.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG):
            yield caplog
    finally:
        ai.logger.removeHandler(caplog.handler)


def _denied():
    return ClientError({"Error": {"Code": "AccessDeniedException",
                                  "Message": f"User: {FAKE_ARN} is not authorized to perform: "
                                             f"bedrock:InvokeModel on resource: {PROFILE_ARN}"}}, "Converse")


def _agent(script=None, **kwargs):
    client = FakeBedrock(script)
    return ai.RelevanceAgent(client=client, sleep=lambda s: None, rng=random.Random(0), **kwargs), client


def _assert_clean(text: str) -> None:
    assert "arn:aws" not in text and FAKE_ACCOUNT not in text, text


def _fatal_from(agent) -> BaseException:
    with pytest.raises(ai.RelevanceAgentFatalError) as exc:
        agent.is_relevant("good cleaner", "cleaner")
    return exc.value


# --- I1: the fatal exception carries no chained AWS error --------------------------------------
@pytest.mark.parametrize("where", ["primary", "fallback"])
def test_fatal_error_is_not_chained_to_the_aws_error(where):
    script = {PRIMARY: [_denied()]} if where == "primary" else {PRIMARY: ["invalid"], FALLBACK: [_denied()]}
    agent, _ = _agent(script)
    exc = _fatal_from(agent)
    assert exc.__cause__ is None and exc.__context__ is None
    assert exc.aws_code == "AccessDeniedException"
    assert exc.model == (PRIMARY if where == "primary" else FALLBACK)
    _assert_clean("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    _assert_clean(str(exc))
    assert "AccessDeniedException" in str(exc), "the AWS code (not its message) is kept for diagnosis"
    # Every later call re-raises the stored error: it must be clean too.
    with pytest.raises(ai.RelevanceAgentFatalError) as again:
        agent.is_relevant("good b", "q")
    assert again.value.__cause__ is None and again.value.__context__ is None
    assert (again.value.aws_code, again.value.model) == (exc.aws_code, exc.model)
    _assert_clean("".join(traceback.format_exception(type(again.value), again.value, again.value.__traceback__)))


def test_a_scraper_logging_with_exc_info_leaks_no_arn(agent_logs):
    """Same pattern as the Selenium scrapers: catch everything and log with exc_info=True."""
    agent, _ = _agent({PRIMARY: [_denied()]})
    scraper_logger = logging.getLogger("wayakit_price_prediction.fake_scraper")
    try:
        agent.is_relevant("good cleaner", "cleaner")
    except Exception:
        scraper_logger.error("Error while evaluating relevance.", exc_info=True)
    try:
        agent.extract_wipes_units("wipes 80")
    except Exception:
        scraper_logger.error("Error while counting wipes.", exc_info=True)
    assert "RelevanceAgentFatalError" in agent_logs.text, "the error must still be logged"
    assert "Bedrock" in agent_logs.text, "the agent's own lines are captured too"
    _assert_clean(agent_logs.text)


def test_client_creation_failure_is_not_chained(monkeypatch):
    import boto3

    class BrokenSession:
        def __init__(self, region_name=None):
            pass

        def get_credentials(self):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": f"{FAKE_ARN} denied"}}, "AssumeRole")

    monkeypatch.setattr(boto3.session, "Session", BrokenSession)
    with pytest.raises(ai.RelevanceAgentFatalError) as exc:
        ai.RelevanceAgent(sleep=lambda s: None)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    _assert_clean("".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)))


# --- nits: network errors are transient -------------------------------------------------------
NETWORK_ERRORS = [
    EndpointConnectionError(endpoint_url="https://bedrock-runtime.eu-north-1.amazonaws.com"),
    ConnectTimeoutError(endpoint_url="https://bedrock-runtime.eu-north-1.amazonaws.com"),
    ReadTimeoutError(endpoint_url="https://bedrock-runtime.eu-north-1.amazonaws.com"),
    ConnectionClosedError(endpoint_url="https://bedrock-runtime.eu-north-1.amazonaws.com"),
    HTTPClientError(error=OSError("fake connection reset")),
]
NETWORK_IDS = ["endpoint-connection", "connect-timeout", "read-timeout", "connection-closed", "http-client"]


@pytest.mark.parametrize("error", NETWORK_ERRORS, ids=NETWORK_IDS)
def test_botocore_network_errors_are_transient(error):
    assert ai.classify_error(error) == "transient"
    agent, client = _agent({PRIMARY: [error] * ai.TRANSIENT_ATTEMPTS})
    assert agent.is_relevant("good cleaner", "cleaner") is True
    assert client.models_called() == [PRIMARY] * ai.TRANSIENT_ATTEMPTS + [FALLBACK]
    assert agent.fatal_error is None


# --- nits: model IDs in logs ------------------------------------------------------------------
def test_logged_model_ids_are_never_a_full_arn(agent_logs, monkeypatch):
    monkeypatch.setenv(ai.PRIMARY_MODEL_ENV, PROFILE_ARN)
    monkeypatch.setenv(ai.FALLBACK_MODEL_ENV, PROFILE_ARN.replace("fakeprofile01", "fakeprofile02"))
    agent, client = _agent({PROFILE_ARN: ["invalid", _denied()]})
    assert agent.primary_model == PROFILE_ARN, "the full ARN is still what is sent to Bedrock"
    agent.is_relevant("good a", "q")  # primary invalid → fallback answers
    with pytest.raises(ai.RelevanceAgentFatalError) as exc:
        agent.is_relevant("good b", "q")  # primary AccessDenied → fatal
    assert client.calls[0]["modelId"] == PROFILE_ARN
    assert "fakeprofile01" in agent_logs.text and "fakeprofile02" in agent_logs.text, "labels are logged"
    _assert_clean(agent_logs.text)
    _assert_clean(str(exc.value))
    assert exc.value.model == "fakeprofile01"


@pytest.mark.parametrize("error, kind", [
    (type("EndpointConnectionError", (Exception,), {})(), "transient"),   # fake matched by class name
    (type("NoCredentialsError", (Exception,), {})(), "fatal"),
    (RuntimeError("bug"), "fatal"),
], ids=["fake-network-by-name", "fake-credentials-by-name", "bug"])
def test_classify_by_class_name_for_fakes_and_bugs_are_fatal(error, kind):
    assert ai.classify_error(error) == kind


# --- model_label ------------------------------------------------------------------------------
@pytest.mark.parametrize("model_id, label", [
    (PROFILE_ARN, "fakeprofile01"),
    (f"arn:aws:bedrock:eu-north-1:{FAKE_ACCOUNT}:foundation-model", "<arn>"),
    ("deepseek.v3.2", "deepseek.v3.2"),
    ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
])
def test_model_label(model_id, label):
    assert ai.model_label(model_id) == label
    assert ai.model_label(label) == label  # idempotent


# --- preflight (review N1) ----------------------------------------------------------------------
@pytest.mark.parametrize("failing", ["primary", "fallback"])
@pytest.mark.parametrize("how", ["invalid", "model-code", "transient"])
def test_preflight_fails_if_either_model_fails_without_switching(failing, how):
    model = PRIMARY if failing == "primary" else FALLBACK
    error = {"invalid": ["invalid"],
             "model-code": [ClientError({"Error": {"Code": "ValidationException", "Message": FAKE_ARN}}, "Converse")],
             "transient": [ClientError({"Error": {"Code": "ThrottlingException", "Message": FAKE_ARN}}, "Converse")]
             * ai.TRANSIENT_ATTEMPTS}[how]
    client = FakeBedrock({model: error})
    sleeps = []
    agent = ai.RelevanceAgent(client=client, sleep=sleeps.append, rng=random.Random(0))
    with pytest.raises(ai.RelevanceAgentFatalError) as exc:
        agent.preflight()
    assert agent.fatal_error is exc.value
    assert exc.value.model == model and exc.value.aws_code == ""
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    _assert_clean("".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)))
    expected_calls = len(error) if how == "transient" else 1
    called = client.models_called()
    if failing == "primary":
        assert called == [PRIMARY] * expected_calls, "no switch to the fallback during preflight"
    else:
        assert called == [PRIMARY] + [FALLBACK] * expected_calls
    assert len(sleeps) == (ai.TRANSIENT_ATTEMPTS - 1 if how == "transient" else 0)
    stats = agent.stats()
    assert stats["evaluated"] == stats["failures"] == stats["fallback_batches"] == 0


def test_preflight_fatal_code_on_the_fallback_sets_aws_code():
    agent, client = _agent({FALLBACK: [_denied()]})
    with pytest.raises(ai.RelevanceAgentFatalError) as exc:
        agent.preflight()
    assert (exc.value.aws_code, exc.value.model) == ("AccessDeniedException", FALLBACK)
    assert client.models_called() == [PRIMARY, FALLBACK]
    _assert_clean(str(exc.value))


def test_preflight_with_the_same_model_calls_it_once():
    agent, client = _agent(fallback_model=PRIMARY)
    agent.preflight()
    assert client.models_called() == [PRIMARY]
