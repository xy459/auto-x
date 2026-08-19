# x-actions-playwright

`x-actions-playwright` is a Python 3.12+ and `playwright.async_api` rewrite of
the 55 canonical actions in `x-action-lab` 1.2.7. It operates on a caller-owned
Playwright `Page`; it does not launch browsers, inject a long-lived JavaScript
runtime, schedule work, or impose business quotas.

The package keeps four boundaries explicit:

1. `catalog.py` is the public action contract: access, retry policy, target,
   idempotency, schemas, failure modes and edge cases.
2. `adapter.py` owns X-specific locators, page objects, navigation, composer
   binding and postcondition checks.
3. `facade.py` applies technical execution policy and exposes
   `await actions.execute(page, action_id, payload, options)` plus namespaces.
4. `workflow.py` composes bounded steps with conditions, cancellation and safe
   retries. It never retries actions whose catalog policy is `never`.

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
business idempotency key.

## Tests

Tests use lightweight Playwright-shaped fakes and DOM-contract fixtures. No
test logs into X or performs a real write.

```bash
python3.12 -m pip install -e '.[dev]'
pytest
ruff check .
mypy src
```
