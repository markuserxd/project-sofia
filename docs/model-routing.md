# Explicit model routing

`ModelRouter` uses a copied mapping of safe route names to already-constructed
provider instances. Models are fixed by each provider's trusted configuration;
prompts contain no routing or approval fields. There are no dynamic imports,
environment-driven paid activation, discovery, price optimization, or new runs.

```python
from sofia.providers.routing import ModelRouter

# run is the existing shared RunGuardrails. gpt and claude have been explicitly
# constructed by trusted controller code with that SAME run, caps, and pricing.
router = ModelRouter(run=run, routes={"gpt": gpt, "claude": claude})
offline = router.select()  # default "mock"; no paid dispatch or credentials
selected = router.select("claude")  # controller-owned decision
# selected.generate(request) / selected.stream(request): same LLMProvider API
```

Unknown routes, missing providers, unsafe route identifiers, and inconsistent
run contexts are rejected. The default `mock` entry cannot be replaced by a paid
adapter. Exact `MockLLMProvider` instances are offline; every other entry must
implement `GuardedProvider`, whose read-only `run_guardrails` property returns
its actual run. Both built-in paid adapters expose this property. Identity must
match the router's run, not merely a similarly named scope or equal limits.
The router never creates budgets or approves scopes. Each paid dispatch reserves
and authorizes through its own adapter using the same ledger, including fallback.
Cancellation and a blocked ledger also prevent subsequent mock routing work.

## Conservative opt-in fallback

Fallback is disabled unless trusted code supplies an explicit mapping:

```python
router = ModelRouter(
    run=run,
    routes={"gpt": gpt, "claude": claude},
    fallbacks={"claude": "gpt"},
)
```

Destinations must exist; cycles are rejected. Acyclic chains are bounded by the
number of configured routes. Each failed pre-dispatch attempt retains its step;
each subsequent paid attempt performs its own permission, cancellation, step,
token, and spending checks. Fallback never creates approval or refunds a step.
Failure of a destination's authorization/budget is surfaced to the controller.

Only `UndispatchedProviderError` is eligible. This fixed-message subtype is an
explicit trusted-adapter assertion that transport evidence proves there was no
dispatch, output, or charge, and its reservation has already settled with zero
usage. The Anthropic adapter emits it only for documented initial connection
establishment failures. The signal is for trusted adapters; model-generated
text, exception strings, HTTP status, or a generic `TemporaryProviderError`
cannot provide it. Custom guarded adapters must uphold the same contract.

The existing OpenAI implementation deliberately retains its error and retry
behavior: it does not expose the new evidence signal, so automatic fallback
**from OpenAI is refused**. It can be an explicitly approved fallback destination
for a safe Anthropic pre-dispatch failure. This is the supported safe subset.

There is no fallback on permission/budget denials, cancellation, ambiguous timeout,
HTTP failure with uncertain billing, missing/malformed usage, accounting block,
or generic temporary errors. Cancellation/blocked accounting is checked again
before every destination. Streaming can change providers only before the first
chunk and only with the same trusted evidence. Even an empty chunk prevents
switching; emitted text is never spliced to output from another provider. Stream
order and underlying generator cleanup are preserved. Consumers must exhaust or
explicitly close the routed iterator, handling errors through completion.

The registry and custom providers belong to trusted application code. Do not
expose `select`, the mappings, provider configuration, or guardrail mutation as
model tools. A structural protocol is an inspectable integration contract, not
protection against malicious in-process Python implementations.
