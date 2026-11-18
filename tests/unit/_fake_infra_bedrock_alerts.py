"""In-memory fakes of the Bedrock and SNS clients used by `infra/bedrock_profiles.py` and
`infra/alerts.py`, plus a fake session. Shared by the unit and property tests of those scripts.

They keep state between calls (a profile created by one run is listed by the next), so a test
can run a script twice and check the second run changes nothing. Every call is recorded in
`calls` as `(operation, params)`. Nothing here reaches AWS; the account is `000000000000`.
"""
from __future__ import annotations

import itertools

from botocore.exceptions import ClientError

from infra import config

ACCOUNT = "000000000000"
# Operations that only read. Anything else is a change.
READ_PREFIXES = ("get_", "list_", "describe_")
PENDING = "PendingConfirmation"


def client_error(code, operation, message="fake message"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def mutating(calls):
    """Operations in `calls` that are not reads."""
    return [op for op, _ in calls if not op.startswith(READ_PREFIXES)]


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages(**kwargs))


class FakeSTS:
    def __init__(self, account=ACCOUNT):
        self.account = account
        self.calls = []

    def get_caller_identity(self):
        self.calls.append(("get_caller_identity", {}))
        return {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:user/fake", "UserId": "FAKE"}


class FakeSession:
    """Hands out the given clients and records every `client()` call with its kwargs."""

    def __init__(self, **clients):
        self.clients = clients
        self.client_calls = []

    def client(self, service, **kwargs):
        self.client_calls.append((service, kwargs))
        return self.clients[service]


class ExplodingSession:
    """A session that must not be used (e.g. invalid input is rejected before any AWS call)."""

    def client(self, service, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError(f"AWS client {service!r} requested; no AWS call was expected")


# --- Bedrock -----------------------------------------------------------------------------------
HAIKU_REGIONS = ("eu-central-1", "eu-north-1", "eu-west-1", "eu-west-3")


def deepseek_models():
    return [{"modelArn": config.foundation_model_arn(config.DEEPSEEK_MODEL_ID)}]


def haiku_models(regions=HAIKU_REGIONS):
    return [{"modelArn": config.foundation_model_arn(config.HAIKU_MODEL_ID, r)} for r in regions]


class FakeBedrock:
    """Application inference profiles in memory.

    `profiles`: `{name: {"arn", "models", "tags": {k: v}}}`. `reject_copy_from`: set of source
    ARNs whose copy AWS rejects with `ValidationException` (the documented DeepSeek fallback)."""

    _ids = itertools.count(1)

    def __init__(self, profiles=None, reject_copy_from=(), account=ACCOUNT):
        self.account = account
        self.profiles = {k: dict(v, tags=dict(v.get("tags", {}))) for k, v in (profiles or {}).items()}
        self.reject_copy_from = set(reject_copy_from)
        self.calls = []

    def add(self, name, models, tags=None):
        arn = (f"arn:aws:bedrock:{config.REGION}:{self.account}:application-inference-profile/"
               f"fake{next(self._ids):06d}")
        self.profiles[name] = {"arn": arn, "models": models, "tags": dict(tags or {})}
        return arn

    def state(self):
        return {k: (v["arn"], tuple(m["modelArn"] for m in v["models"]), tuple(sorted(v["tags"].items())))
                for k, v in self.profiles.items()}

    def _by_arn(self, arn):
        for p in self.profiles.values():
            if p["arn"] == arn:
                return p
        raise client_error("ResourceNotFoundException", "ListTagsForResource")

    def get_paginator(self, operation):
        assert operation == "list_inference_profiles", operation

        def pages(**kwargs):
            self.calls.append(("list_inference_profiles", kwargs))
            assert kwargs == {"typeEquals": "APPLICATION"}, kwargs
            summaries = [{"inferenceProfileName": name, "inferenceProfileArn": p["arn"],
                          "models": list(p["models"]), "type": "APPLICATION", "status": "ACTIVE"}
                         for name, p in self.profiles.items()]
            # Two pages, so a script that reads only the first page is caught.
            half = len(summaries) // 2
            return [{"inferenceProfileSummaries": summaries[:half], "nextToken": "t"},
                    {"inferenceProfileSummaries": summaries[half:]}]

        return _Paginator(pages)

    def list_tags_for_resource(self, **kwargs):
        self.calls.append(("list_tags_for_resource", kwargs))
        tags = self._by_arn(kwargs["resourceARN"])["tags"]
        return {"tags": [{"key": k, "value": v} for k, v in tags.items()]}

    def tag_resource(self, **kwargs):
        self.calls.append(("tag_resource", kwargs))
        p = self._by_arn(kwargs["resourceARN"])
        for t in kwargs["tags"]:
            p["tags"][t["key"]] = t["value"]
        return {}

    def create_inference_profile(self, **kwargs):
        self.calls.append(("create_inference_profile", kwargs))
        source = kwargs["modelSource"]["copyFrom"]
        if source in self.reject_copy_from:
            raise client_error("ValidationException", "CreateInferenceProfile")
        if kwargs["inferenceProfileName"] in self.profiles:
            raise client_error("ConflictException", "CreateInferenceProfile")
        models = deepseek_models() if source.endswith(config.DEEPSEEK_MODEL_ID) else haiku_models()
        arn = self.add(kwargs["inferenceProfileName"], models,
                       {t["key"]: t["value"] for t in kwargs.get("tags", [])})
        return {"inferenceProfileArn": arn, "status": "ACTIVE"}


# --- SNS ---------------------------------------------------------------------------------------
class FakeSNS:
    """One SNS topic (or none) in memory. `subs`: list of `{"Protocol", "Endpoint", "confirmed"}`."""

    _ids = itertools.count(1)

    def __init__(self, exists=False, attributes=None, tags=None, subs=None, account=ACCOUNT):
        self.account = account
        self.arn = config.topic_arn(account)
        self.exists = exists
        self.attributes = dict(attributes or {})
        self.tags = dict(tags or {})
        self.subs = [dict(s) for s in subs or []]
        self.calls = []

    def state(self):
        return (self.exists, tuple(sorted(self.attributes.items())), tuple(sorted(self.tags.items())),
                tuple((s["Protocol"], s["Endpoint"], s["confirmed"]) for s in self.subs))

    def _check(self, arn, operation):
        if not self.exists or arn != self.arn:
            raise client_error("NotFound", operation)

    def get_topic_attributes(self, **kwargs):
        self.calls.append(("get_topic_attributes", kwargs))
        self._check(kwargs["TopicArn"], "GetTopicAttributes")
        attrs = {"TopicArn": self.arn, "Owner": self.account,
                 "SubscriptionsConfirmed": str(sum(s["confirmed"] for s in self.subs)),
                 "SubscriptionsPending": str(sum(not s["confirmed"] for s in self.subs))}
        attrs.update(self.attributes)
        return {"Attributes": attrs}

    def create_topic(self, **kwargs):
        self.calls.append(("create_topic", kwargs))
        if not self.exists:
            self.exists = True
            self.attributes = dict(kwargs.get("Attributes", {}))
            self.tags = {t["Key"]: t["Value"] for t in kwargs.get("Tags", [])}
        return {"TopicArn": self.arn}

    def set_topic_attributes(self, **kwargs):
        self.calls.append(("set_topic_attributes", kwargs))
        self._check(kwargs["TopicArn"], "SetTopicAttributes")
        self.attributes[kwargs["AttributeName"]] = kwargs["AttributeValue"]
        return {}

    def list_tags_for_resource(self, **kwargs):
        self.calls.append(("list_tags_for_resource", kwargs))
        self._check(kwargs["ResourceArn"], "ListTagsForResource")
        return {"Tags": [{"Key": k, "Value": v} for k, v in self.tags.items()]}

    def tag_resource(self, **kwargs):
        self.calls.append(("tag_resource", kwargs))
        self._check(kwargs["ResourceArn"], "TagResource")
        for t in kwargs["Tags"]:
            self.tags[t["Key"]] = t["Value"]
        return {}

    def get_paginator(self, operation):
        assert operation == "list_subscriptions_by_topic", operation

        def pages(**kwargs):
            self.calls.append(("list_subscriptions_by_topic", kwargs))
            self._check(kwargs["TopicArn"], "ListSubscriptionsByTopic")
            subs = [{"SubscriptionArn": f"{self.arn}:fake-{i}" if s["confirmed"] else PENDING,
                     "Owner": self.account, "Protocol": s["Protocol"], "Endpoint": s["Endpoint"],
                     "TopicArn": self.arn} for i, s in enumerate(self.subs)]
            half = len(subs) // 2
            return [{"Subscriptions": subs[:half], "NextToken": "t"}, {"Subscriptions": subs[half:]}]

        return _Paginator(pages)

    def subscribe(self, **kwargs):
        self.calls.append(("subscribe", kwargs))
        self._check(kwargs["TopicArn"], "Subscribe")
        self.subs.append({"Protocol": kwargs["Protocol"], "Endpoint": kwargs["Endpoint"], "confirmed": False})
        return {"SubscriptionArn": "pending confirmation"}
