# x-actions-playwright

`x-actions-playwright` is a Python 3.12+ and `playwright.async_api` rewrite of
the 55 canonical actions in `x-action-lab` 1.2.7. It operates on a caller-owned
Playwright `Page`; it does not launch browsers, inject a long-lived JavaScript
runtime, schedule work, or impose business quotas.

The package keeps three boundaries explicit:

1. `catalog.py` is the public action contract: access, retry policy, target,
   idempotency, schemas, failure modes and edge cases.
2. `adapter.py` owns X-specific locators, page objects, navigation, composer
   binding and postcondition checks.
3. `facade.py` applies technical execution policy and exposes
   `await actions.execute(page, action_id, payload, options)` plus namespaces.

It intentionally contains no task orchestration, scheduler, account selection,
browser lifecycle, business matching, AI generation, or TaskRun persistence.
Callers compose atomic actions in their own task programs.

## Usage

```python
from x_actions_playwright import XActions

actions = XActions()

timeline = await actions.timeline.collect(
    page,
    {
        "feed": "for-you",
        "durationMs": 8_000,
        "maxScrolls": 10,
        "maxPosts": 30,
        "includeAds": False,
    },
)

result = await actions.interaction.like(
    page,
    {"tweetId": "123456789"},
    {
        "confirmLive": True,
        "idempotencyKey": "job-42:like:123456789",
        "accountScope": "account-42",
    },
)
```

`ActionResult.status` is one of `success`, `skipped`, `navigating`,
`uncertain`, `cancelled`, or `failed`. Invalid preconditions raise
`ActionError`, whose `to_dict()` output is stable and machine-readable.

## Execution rules

- Playwright `Locator` performs clicks, fills, typing, navigation and waits.
- Small, stateless `evaluate`/`evaluate_all` calls are used for bulk DOM reads,
  video element properties, and scroll positions. No action bundle is injected.
- Main-post controls exclude descendants of `[data-testid="quoteTweet"]`.
- Menus and dialogs are re-located at click time because X mounts them in
  portals and frequently replaces their nodes.
- A write that was clicked but whose final state cannot be proven returns
  `uncertain`; it is never automatically retried.
- `confirmLive`, `dryRun`, and `captureFailure` accept only real Python/JSON
  booleans. Strings such as `"false"`, integers, and `null` are rejected with
  `CONTENT_MISMATCH`.
- `dryRun` validates and identifies targets without changing the account.
- `publish.post` and `publish.schedule` use Playwright `press_sequentially` and
  verify both editor text and enabled submission controls. Unsupported layouts
  fail closed with `ACTION_UNSUPPORTED` or `CONTENT_MISMATCH`.
- Login, challenge and restricted-account screens are reported as structured
  errors. The library does not solve or bypass them.

## Idempotency

Pass an implementation of `IdempotencyStore` to `XActions`. The default
`MemoryIdempotencyStore` is only process-local. A production caller should
provide a database-backed store keyed by account/profile plus the supplied
business idempotency key. `claim()` must reserve a key atomically; a separate
`get()` followed by `put()` is not sufficient under concurrency. When an
`idempotencyKey` is supplied, `accountScope` is required to prevent keys from
colliding across accounts.

An existing `pending` reservation raises the retryable
`IDEMPOTENCY_IN_PROGRESS` error instead of pretending the second execution was
skipped. An existing `uncertain` outcome is returned as `uncertain`; only a
previous `success` or `skipped` outcome produces `idempotency-key-reused`.
Unknown or failed stored states raise `IDEMPOTENCY_STATE_CONFLICT`. The library
does not automatically reclaim stale `pending` reservations: a persistent
store must implement its own operator-reviewed lease/TTL recovery policy.

## Cancellation and uncertain writes

`ExecutionOptions.cancellation` accepts an event-shaped signal exposing
`is_set()` and async `wait()`. Cancellation is checked before an action and
while waiting for the caller-owned `Page` lock. Once a live write starts, the
library finishes its postcondition check instead of treating cancellation as
proof that the write did not happen.

Each call records `dispatchStarted`, `mutationTriggered`, and
`postconditionVerified` in `ActionResult.meta.executionTrace`. Opening menus,
finding controls, filling an editor, or uploading media does not mark a
mutation. Official adapters mark `mutationTriggered` only at the final write
control after a Playwright actionability-only trial click.

If a live write is interrupted by a hard timeout, browser closure, navigation,
or task cancellation after `mutationTriggered`, and the final state cannot be
proven, the result is `uncertain`. Failures before that boundary remain
deterministic errors and release the idempotency reservation. An uncertain
reservation is retained so callers do not blindly repeat a possibly completed
write.

## Tests

Tests use lightweight Playwright-shaped fakes and DOM-contract fixtures. No
test logs into X or performs a real write.

```bash
python3.12 -m pip install -e '.[dev]'
pytest
ruff check .
mypy src
```
