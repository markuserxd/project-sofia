from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from unittest.mock import Mock

import httpx
import httpx2
import openai
import pytest
from anthropic_helpers import REQUEST, EventBody, configuration, events

from sofia.config import ConfigurationError
from sofia.providers import (
    InvalidRequestError,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMStreamChunk,
    MockLLMProvider,
    ProviderError,
    TemporaryProviderError,
)
from sofia.providers.base import UndispatchedProviderError
from sofia.providers.openai_accounting import ModelPricing
from sofia.providers.openai_provider import OpenAIConfig, OpenAIProvider
from sofia.providers.routing import ModelRouter
from sofia.security import (
    AccountingBlocked,
    Action,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    StepLimitExceeded,
    TokenBudgetExceeded,
)


def run_context(**changes):
    return RunGuardrails(
        **({"max_steps": 5, "max_tokens": 1000, "max_usd": Decimal(1)} | changes)
    )


def connect_failure(req, run):
    raise httpx2.ConnectError("private-transport-detail", request=req)


def test_default_offline_and_explicit_routes(claude):
    provider, run, calls = claude()
    router = ModelRouter(run=run, routes={"claude": provider})
    default: LLMProvider = router.select()
    assert default.generate(REQUEST).content == "Hello from SOFIA"
    assert calls == []
    assert router.select("claude").generate(REQUEST).content == "private-answer ไทย🙂"
    with pytest.raises(InvalidRequestError):
        router.select("unknown")
    assert run.budget.snapshot.steps == 1


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_safe_fallback_and_disabled_default(claude, stream):
    primary, run, calls = claude(connect_failure)
    router = ModelRouter(run=run, routes={"claude": primary})

    def invoke(router):
        selected = router.select("claude")
        return (
            "".join(c.content for c in selected.stream(REQUEST))
            if stream
            else selected.generate(REQUEST).content
        )

    with pytest.raises(UndispatchedProviderError):
        invoke(router)
    router = ModelRouter(
        run=run, routes={"claude": primary}, fallbacks={"claude": "mock"}
    )
    assert invoke(router) == "Hello from SOFIA"
    assert run.budget.snapshot.steps == 2
    assert run.budget.snapshot.committed_tokens == 0
    assert not run.budget.snapshot.blocked


@pytest.mark.parametrize(
    "condition,error",
    [
        ("permission", PermissionDenied),
        ("steps", StepLimitExceeded),
        ("cancel", OperationCancelled),
        ("timeout", TemporaryProviderError),
        ("unknown", TemporaryProviderError),
    ],
)
def test_denials_and_ambiguous_failures_do_not_fallback(claude, condition, error):
    def fail(req, run):
        if condition == "timeout":
            raise httpx2.ReadTimeout("private", request=req)
        return httpx2.Response(
            500, json={"error": {"type": "api_error", "message": "private"}}
        )

    provider, run, calls = claude(
        fail,
        approve=condition != "permission",
        max_steps=0 if condition == "steps" else 5,
    )
    if condition == "cancel":
        run.cancellation.cancel()
    router = ModelRouter(
        run=run, routes={"claude": provider}, fallbacks={"claude": "mock"}
    )
    with pytest.raises(error):
        router.select("claude").generate(REQUEST)


def test_generic_temporary_error_does_not_authorize_fallback():
    run = run_context()
    router = ModelRouter(
        run=run,
        routes={"broken": MockLLMProvider(failure="temporary")},
        fallbacks={"broken": "mock"},
    )
    with pytest.raises(TemporaryProviderError):
        router.select("broken").generate(REQUEST)


def test_invalid_configuration_missing_instances_and_inconsistent_runs(claude):
    provider, run, _ = claude()
    other = run_context()
    for routes, fallbacks in [
        ({"missing": None}, None),
        ({"bad name": MockLLMProvider()}, None),
        ({"paid": provider}, {"paid": "missing"}),
        ({"mock": provider}, None),
        ({"paid": provider}, {"paid": "mock", "mock": "paid"}),
    ]:
        with pytest.raises(ConfigurationError):
            ModelRouter(run=run, routes=routes, fallbacks=fallbacks)
    with pytest.raises(ConfigurationError):
        ModelRouter(run=other, routes={"paid": provider})


def test_safe_paid_fallback_must_obtain_its_own_approval_and_step(claude):
    primary, run, calls = claude(connect_failure)
    fallback, _, fallback_calls = claude(
        run=run,
        config=configuration(model="claude-sonnet-4-6"),
        approve=False,
    )
    router = ModelRouter(
        run=run,
        routes={"primary": primary, "fallback": fallback},
        fallbacks={"primary": "fallback"},
    )
    with pytest.raises(PermissionDenied):
        router.select("primary").generate(REQUEST)
    assert not fallback_calls
    assert run.budget.snapshot.steps == 1


def test_no_fallback_after_cancellation_during_connect_failure(claude):
    def stop(req, run):
        run.cancellation.cancel()
        raise httpx2.ConnectError("private", request=req)

    provider, run, calls = claude(stop)
    router = ModelRouter(
        run=run, routes={"claude": provider}, fallbacks={"claude": "mock"}
    )
    with pytest.raises(OperationCancelled):
        router.select("claude").generate(REQUEST)
    assert run.budget.snapshot.steps == 1
    assert not run.budget.snapshot.blocked


def test_partial_stream_never_falls_back_and_explicit_close_cleans_up(claude):
    body = EventBody(events()[:3], failure=httpx2.ReadError("private"))
    provider, run, calls = claude(lambda req, run: httpx2.Response(200, stream=body))
    router = ModelRouter(
        run=run, routes={"claude": provider}, fallbacks={"claude": "mock"}
    )
    iterator = router.select("claude").stream(REQUEST)
    assert next(iterator).content == "private-answer "
    with pytest.raises(ProviderError):
        next(iterator)
    assert body.closed
    assert run.budget.snapshot.blocked


def test_no_fallback_after_even_an_empty_chunk():
    run = run_context()

    class MisbehavingTrustedProvider:
        run_guardrails = run

        def generate(self, request):
            raise UndispatchedProviderError()

        def stream(self, request):
            yield LLMStreamChunk("")
            raise UndispatchedProviderError()

    router = ModelRouter(
        run=run,
        routes={"test": MisbehavingTrustedProvider()},
        fallbacks={"test": "mock"},
    )
    iterator = router.select("test").stream(REQUEST)
    assert next(iterator).content == ""
    with pytest.raises(UndispatchedProviderError):
        next(iterator)


def test_openai_and_anthropic_share_one_ledger(claude):
    run = run_context(
        max_tokens=159
    )  # OpenAI 32 actual + Anthropic 132 reservation exceeds cap.
    model = "gpt-4.1-mini-2025-04-14"
    response = {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "service_tier": "default",
        "error": None,
        "incomplete_details": None,
        "output": [],
        "usage": {"input_tokens": 30, "output_tokens": 2, "total_tokens": 32},
    }
    client = openai.OpenAI(
        api_key="fake",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=response)
            )
        ),
    )
    try:
        gpt = OpenAIProvider(
            config=OpenAIConfig(
                model=model,
                pricing=ModelPricing(model, Decimal("0.4"), Decimal("1.6")),
                input_token_bound=lambda request: 100,
                max_output_tokens=32,
            ),
            run=run,
            client=client,
        )
        claude_provider, _, calls = claude(run=run)
        router = ModelRouter(run=run, routes={"gpt": gpt, "claude": claude_provider})
        run.permissions.approve(Action.API_REQUEST, gpt.permission_scope)
        router.select("gpt").generate(LLMRequest((LLMMessage("user", "hello"),)))
        with pytest.raises(TokenBudgetExceeded):
            router.select("claude").generate(REQUEST)
        assert calls == []
        assert run.budget.snapshot.steps == 1
        assert run.budget.snapshot.committed_tokens == 32
        assert gpt.run_guardrails is claude_provider.run_guardrails is run
    finally:
        client.close()


def test_accounting_block_also_prevents_mock_fallback_work():
    run = run_context()
    reservation = run.budget.reserve(estimated_tokens=1, estimated_usd=Decimal("0.1"))
    with pytest.raises(AccountingBlocked):
        run.budget.settle(reservation, actual_tokens=None, actual_usd=None)
    with pytest.raises(AccountingBlocked):
        ModelRouter(run=run).select().generate(REQUEST)


def openai_fake(run):
    model = "gpt-4.1-mini-2025-04-14"
    client = Mock()
    client.with_options.return_value = client
    client.responses.create.return_value = (
        openai.types.responses.Response.model_construct(
            **{
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1,
                "model": model,
                "status": "completed",
                "service_tier": "default",
                "error": None,
                "incomplete_details": None,
                "output": [],
                "usage": {"input_tokens": 30, "output_tokens": 2, "total_tokens": 32},
            }
        )
    )
    provider = OpenAIProvider(
        config=OpenAIConfig(
            model=model,
            pricing=ModelPricing(model, Decimal("0.4"), Decimal("1.6")),
            input_token_bound=lambda request: 100,
            max_output_tokens=32,
        ),
        run=run,
        client=client,
    )
    run.permissions.approve(Action.API_REQUEST, provider.permission_scope)
    return provider, client


def test_safe_anthropic_to_openai_fallback_shares_budget(claude):
    primary, run, calls = claude(connect_failure)
    gpt, client = openai_fake(run)
    router = ModelRouter(
        run=run, routes={"claude": primary, "gpt": gpt}, fallbacks={"claude": "gpt"}
    )
    assert router.select("claude").generate(REQUEST).content == ""
    assert run.budget.snapshot.steps == 2
    assert run.budget.snapshot.committed_tokens == 32
    assert not run.budget.snapshot.blocked
    assert len(calls) == client.responses.create.call_count == 1


@pytest.mark.parametrize(
    "limits,error",
    [({"max_steps": 1}, StepLimitExceeded), ({"max_tokens": 131}, TokenBudgetExceeded)],
)
def test_paid_fallback_has_no_fresh_step_or_token_budget(claude, limits, error):
    # A smaller first reservation allows the source attempt but not GPT fallback.
    primary, run, calls = claude(
        connect_failure,
        config=configuration(input_token_bound=lambda req: 1, max_output_tokens=1),
        **limits,
    )
    gpt, client = openai_fake(run)
    router = ModelRouter(
        run=run, routes={"claude": primary, "gpt": gpt}, fallbacks={"claude": "gpt"}
    )
    with pytest.raises(error):
        router.select("claude").generate(REQUEST)
    assert run.budget.snapshot.steps == 1
    assert len(calls) == 1
    client.responses.create.assert_not_called()


def test_openai_and_anthropic_concurrency_cannot_oversubscribe(claude):
    admitted, release = Event(), Event()

    def handler(req, run):
        admitted.set()
        assert release.wait(5)
        from anthropic_helpers import payload

        return httpx2.Response(200, json=payload())

    primary, run, calls = claude(handler, max_tokens=132)
    gpt, client = openai_fake(run)
    router = ModelRouter(run=run, routes={"claude": primary, "gpt": gpt})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(router.select("claude").generate, REQUEST)
        assert admitted.wait(5)
        try:
            with pytest.raises(TokenBudgetExceeded):
                router.select("gpt").generate(REQUEST)
        finally:
            release.set()
        first.result(5)
    client.responses.create.assert_not_called()
    assert run.budget.snapshot.committed_tokens == 27


@pytest.mark.parametrize("close", [False, True])
def test_routed_stream_cancellation_and_early_close_preserve_accounting(claude, close):
    body = EventBody(events())
    provider, run, calls = claude(lambda req, run: httpx2.Response(200, stream=body))
    iterator = (
        ModelRouter(run=run, routes={"claude": provider})
        .select("claude")
        .stream(REQUEST)
    )
    next(iterator)
    if close:
        with pytest.raises(AccountingBlocked):
            iterator.close()
    else:
        run.cancellation.cancel()
        with pytest.raises(OperationCancelled) as error:
            next(iterator)
        assert isinstance(error.value.__cause__, AccountingBlocked)
    assert body.closed
    assert run.budget.snapshot.blocked
    assert run.budget.snapshot.steps == 1


@pytest.mark.parametrize(
    "bad", [{"fallbacks": {"mock": []}}, {"fallbacks": []}, {"routes": []}]
)
def test_malformed_registry_settings_are_sanitized(bad):
    with pytest.raises(ConfigurationError):
        ModelRouter(run=run_context(), **bad)
