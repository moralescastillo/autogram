"""The pipeline: one pass, executed hourly.

Read top to bottom, this is the specification of what a run does. The ordering
matters and most of the safety properties come from it rather than from any one
component:

1. Load config, storage and the posting policy.
2. Load the token; refresh it if it is due. A dead token stops everything.
3. **Recover any unfinished publish** — before scheduling, because recovering a
   published post writes the ledger, and the ledger decides whether today's
   slot is already used.
4. Publish anything in ``now/``, ignoring cadence.
5. Otherwise ask the scheduler whether a queued post is due, and publish one.

Exit codes matter, because a non-zero exit is what makes GitHub email the user:

===  ==========================================================
  0  Nothing due, published, or waiting on a slow video
  1  A post failed and needs the user to change something
  2  Configuration is wrong
  3  The token is dead, or state is unreadable — nothing can run
===  ==========================================================
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import yaml

from autogram import config as config_mod
from autogram.config import ConfigError
from autogram.content.post import PostError, discover
from autogram.instagram.client import InstagramClient
from autogram.instagram.errors import Disposition, InstagramError
from autogram.pipeline.context import NOW_ROOT, QUEUE_ROOT, Context
from autogram.pipeline.publish import Result, publish_post, sweep_staging
from autogram.pipeline.recover import resolve_pending
from autogram.scheduling.policy import PolicyError
from autogram.scheduling.policy import parse as parse_policy
from autogram.scheduling.scheduler import is_due, next_slot
from autogram.state import log as events
from autogram.state.ledger import Ledger, LedgerCorrupt
from autogram.state.log import EventLog
from autogram.state.pending import PendingStore
from autogram.state.token import TokenStore
from autogram.storage import from_dsn

log = logging.getLogger(__name__)

POLICY_FILE = "autogram.yml"

EXIT_OK = 0
EXIT_POST_FAILED = 1
EXIT_CONFIG = 2
EXIT_BLOCKED = 3


def load_policy(storage):
    """Read ``autogram.yml`` from the storage root, or use the defaults."""
    try:
        raw = storage.read(POLICY_FILE).decode("utf-8")
    except FileNotFoundError:
        log.info("No %s found; using default schedule.", POLICY_FILE)
        return parse_policy(None)

    try:
        return parse_policy(yaml.safe_load(raw))
    except yaml.YAMLError as exc:
        raise PolicyError(f"{POLICY_FILE} is not valid YAML: {exc}") from exc


def build_context(config, *, now: datetime | None = None, dry_run: bool = False) -> Context:
    """Assemble everything a run needs."""
    authoring = from_dsn(config.authoring_dsn)
    serving = authoring if not config.is_split else from_dsn(config.serving_dsn)

    if not serving.can_serve and not dry_run:
        # A dry run never hands Instagram a URL, so it is allowed to preview a
        # setup that could not yet publish — which is exactly when someone
        # wants to check their content is valid.
        raise ConfigError(
            "The configured storage cannot serve media to Instagram. Add a "
            "gs:// or s3:// connection string as the second line of "
            "AUTOGRAM_STORAGE."
        )

    now = now or datetime.now(timezone.utc)
    policy = load_policy(authoring)

    token_store = TokenStore(authoring)
    token = token_store.load(config.ig_bootstrap_token, config.ig_user_id, now=now)

    client = InstagramClient(token.access_token, config.ig_user_id)

    if not dry_run:
        token, changed = token_store.refresh_if_needed(token, client, now=now)
        if changed:
            client = InstagramClient(token.access_token, config.ig_user_id)

    ctx = Context(
        authoring=authoring,
        serving=serving,
        client=client,
        ledger=Ledger(authoring),
        pending=PendingStore(authoring),
        events=EventLog(authoring),
        policy=policy,
        ig_user_id=config.ig_user_id,
        now=now,
        dry_run=dry_run,
    )

    if not dry_run and token.days_remaining(now) < 7:
        ctx.events.emit(
            events.TOKEN_WARNING,
            days_remaining=round(token.days_remaining(now), 1),
            now=now,
        )

    return ctx


def _discover(ctx: Context, root: str, *, immediate: bool):
    """List posts under a root, explaining broken ones without stopping."""

    def note_broken(folder: str, exc: PostError) -> None:
        name = folder.rstrip("/").rsplit("/", 1)[-1]
        log.warning("Skipping '%s': %s", name, exc)
        if ctx.dry_run:
            return
        # Say so where the user will see it, rather than only in a log they
        # would have to go looking for.
        text = (
            f"Autogram could not read '{name}'.\n\n"
            f"Fix the following and it will be picked up on the next run:\n\n"
            f"  - post.md {exc}\n"
        )
        try:
            ctx.authoring.write(ctx.error_path(folder), text.encode("utf-8"))
            ctx.events.emit(events.FAILED, post=name, problems=[str(exc)], now=ctx.now)
        except Exception:  # pragma: no cover - defensive
            pass

    try:
        return discover(ctx.authoring, root, immediate=immediate, on_error=note_broken)
    except Exception as exc:
        log.warning("Could not read %s: %s", root, exc)
        return []


def run(*, dry_run: bool = False, now: datetime | None = None) -> int:
    """Execute one pass. Returns a process exit code."""
    try:
        config = config_mod.load(dry_run=dry_run)
    except ConfigError as exc:
        if config_mod.looks_unconfigured():
            # A freshly forked repository has no secrets yet. That is not a
            # failure — it has nothing to do. Failing here would email the
            # owner every hour until they finish setting up, which is exactly
            # the noise that teaches people to ignore these emails.
            print("Autogram is not configured yet, so there is nothing to do.")
            print()
            print("Add these repository secrets to start posting:")
            print("  AUTOGRAM_STORAGE      where your content lives")
            print("  AUTOGRAM_IG_TOKEN     your Instagram access token")
            print("  AUTOGRAM_IG_USER_ID   your Instagram account id")
            print()
            print("See docs/SETUP.md. Until then this workflow will do nothing.")
            return EXIT_OK
        raise

    try:
        ctx = build_context(config, now=now, dry_run=dry_run)
    except PolicyError as exc:
        print(f"Configuration error in {POLICY_FILE}: {exc}")
        return EXIT_CONFIG
    except InstagramError as exc:
        if exc.disposition is Disposition.AUTH:
            print(f"Instagram rejected the access token: {exc}")
            print(
                "Generate a new long-lived token and update the "
                "AUTOGRAM_IG_TOKEN secret; the next run will pick it up."
            )
            return EXIT_BLOCKED
        raise

    log.info("Schedule: %s", ctx.policy.describe())

    try:
        return _run_with_context(ctx, dry_run=dry_run)
    except LedgerCorrupt as exc:
        print(f"Cannot continue: {exc}")
        return EXIT_BLOCKED
    except InstagramError as exc:
        if exc.disposition is Disposition.AUTH:
            ctx.events.emit(events.TOKEN_WARNING, reason=str(exc), now=ctx.now)
            print(f"Instagram rejected the access token: {exc}")
            return EXIT_BLOCKED
        raise


def _run_with_context(ctx: Context, *, dry_run: bool) -> int:
    immediate_posts = _discover(ctx, NOW_ROOT, immediate=True)
    queued_posts = _discover(ctx, QUEUE_ROOT, immediate=False)

    if dry_run:
        return _report_dry_run(ctx, immediate_posts, queued_posts)

    # Recovery first: it may write the ledger, which changes what is due.
    for post in immediate_posts + queued_posts:
        outcome = resolve_pending(ctx, post)
        if outcome is not None:
            log.info("Resolved unfinished publish for '%s': %s", post.name, outcome.result.value)
            if outcome.result is Result.PUBLISHED:
                return _finish(ctx, outcome, immediate_posts + queued_posts)
            if outcome.result is Result.RETRY_LATER:
                return _finish(ctx, outcome, immediate_posts + queued_posts)
            if outcome.result is Result.FAILED:
                return _finish(ctx, outcome, immediate_posts + queued_posts)

    if immediate_posts:
        post = immediate_posts[0]
        log.info("Publishing '%s' immediately (found in %s/).", post.name, NOW_ROOT)
        return _finish(ctx, publish_post(ctx, post), immediate_posts + queued_posts)

    # An immediate post is by definition outside the cadence, so it does not
    # consume the day's scheduled slot.
    decision = is_due(
        ctx.policy,
        ctx.now,
        ctx.ig_user_id,
        last_published=ctx.ledger.last_published(scheduled_only=True),
    )

    if not decision.due:
        log.info("Nothing to do — %s.", decision.reason)
        return EXIT_OK

    if not queued_posts:
        log.info("Due to post, but %s/ is empty.", QUEUE_ROOT)
        return EXIT_OK

    post = queued_posts[0]
    log.info("Publishing '%s' — %s.", post.name, decision.reason)
    return _finish(ctx, publish_post(ctx, post), immediate_posts + queued_posts)


def _finish(ctx: Context, outcome, posts) -> int:
    live = {p.name for p in posts}
    sweep_staging(ctx, live)

    if outcome.result is Result.PUBLISHED:
        log.info("Published '%s' (media %s).", outcome.post, outcome.media_id or "recovered")
        return EXIT_OK

    if outcome.result is Result.RETRY_LATER:
        log.info("'%s' is not ready yet; will retry. %s", outcome.post, outcome.detail)
        return EXIT_OK

    if outcome.result is Result.FAILED:
        log.error("Could not publish '%s':", outcome.post)
        for problem in outcome.problems:
            log.error("  - %s", problem)
        log.error("An error.txt explaining this has been written to the post's folder.")
        return EXIT_POST_FAILED

    return EXIT_OK


def _report_dry_run(ctx: Context, immediate_posts, queued_posts) -> int:
    """Say what would happen, changing nothing."""
    from autogram.content.validate import load_media, validate

    print()
    print(f"Schedule:  {ctx.policy.describe()}")
    print(f"Now:       {ctx.now.astimezone(ctx.policy.timezone):%Y-%m-%d %H:%M} {ctx.policy.timezone_name}")

    try:
        account = ctx.client.whoami()
        print(f"Account:   @{account.get('username', '?')} ({account.get('id', '?')})")
    except InstagramError as exc:
        print(f"Account:   could not verify — {exc}")

    last = ctx.ledger.last_published(scheduled_only=True)
    print(f"Last post: {last:%Y-%m-%d %H:%M} UTC" if last else "Last post: never")

    decision = is_due(ctx.policy, ctx.now, ctx.ig_user_id, last_published=last)
    print(f"Due now:   {'yes' if decision.due else 'no'} — {decision.reason}")

    upcoming = next_slot(ctx.policy, ctx.now, ctx.ig_user_id, last)
    print(f"Next slot: {upcoming:%a %Y-%m-%d %H:%M} {ctx.policy.timezone_name}")
    print()

    for post in immediate_posts:
        print(f"{NOW_ROOT}/{post.name} — would publish immediately")

    print(f"{QUEUE_ROOT}/: {len(queued_posts)} post(s)")
    for index, post in enumerate(queued_posts):
        marker = " <- next" if index == 0 else ""
        print(f"  {post.name} ({post.resolved_type.value}, {len(post.media)} file(s)){marker}")

    candidate = (immediate_posts or queued_posts or [None])[0]
    if candidate is not None:
        print()
        print(f"Checking '{candidate.name}':")

        pending = ctx.pending.read(candidate.folder)
        if pending:
            print(f"  unfinished publish found (container {pending.container_id})")

        items, problems = load_media(ctx.authoring, candidate)
        if items:
            problems.problems.extend(validate(candidate, items).problems)

        if problems.ok:
            print("  all checks passed")
            for item in items:
                size = f"{item.width}x{item.height}" if item.width else "size unknown"
                print(f"    {item.name} -> {item.upload_name} ({size}, {item.size / 1024:.0f}KB)")
        else:
            for problem in problems.problems:
                print(f"  - {problem}")

    print()
    print("Dry run: nothing was published, moved or written.")
    return EXIT_OK
