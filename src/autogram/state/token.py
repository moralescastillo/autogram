"""The Instagram token, and keeping it alive.

This is the problem the previous system never solved: long-lived Instagram
tokens expire after 60 days, and one left unrefreshed for 60 days is dead
permanently — there is no recovery except issuing a new one by hand.

**Why the token lives in storage rather than a GitHub secret.** Refreshing
returns a *new* token string. A workflow's ``GITHUB_TOKEN`` cannot write
repository secrets, so a self-refreshing secret would require the user to mint
an additional personal access token with elevated permissions — an onboarding
burden and a security smell. Instead the GitHub secret is a *bootstrap*, read
once, and the live token lives in ``state/token.json`` alongside the content.

**The unknown-age problem.** A bootstrap token's age cannot be determined from
the token itself: it might have been minted a minute ago or seven weeks ago. So
on seeding we assume the pessimistic-but-typical 60 days and mark the record
*unconfirmed*. Once the token is old enough to refresh — Meta requires 24 hours
— the next run refreshes it regardless of how far off expiry looks, which
replaces the assumption with an exact lifetime from Meta.

The only case this loses is a bootstrap token already near expiry, and it loses
loudly: the run fails and GitHub emails the user.

**Ordering.** The refreshed token is written to storage before it is used. If
the write fails, the run fails with the old token still on record — Meta keeps
the previous token valid until its original expiry, so the next run simply
tries again.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from autogram.instagram.errors import Disposition, InstagramError

log = logging.getLogger(__name__)

TOKEN_PATH = "state/token.json"

#: Meta issues long-lived tokens with a 60-day life.
ASSUMED_LIFETIME = timedelta(days=60)

#: Refresh once expiry is closer than this. The workflow runs hourly, so this
#: allows ~168 attempts before the token dies.
REFRESH_THRESHOLD = timedelta(days=7)

#: Meta refuses to refresh a token younger than this.
MIN_TOKEN_AGE = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(secret: str) -> str:
    """Identify a token without storing it. Used to notice a changed secret."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


@dataclass
class TokenInfo:
    access_token: str
    expires_at: datetime
    refreshed_at: datetime | None = None
    seeded_at: datetime | None = None
    confirmed: bool = False
    bootstrap_fingerprint: str = ""
    ig_user_id: str = ""

    def days_remaining(self, now: datetime | None = None) -> float:
        return (self.expires_at - (now or _now())).total_seconds() / 86400

    def needs_refresh(self, now: datetime | None = None) -> bool:
        now = now or _now()

        age_reference = self.refreshed_at or self.seeded_at
        if age_reference and now - age_reference < MIN_TOKEN_AGE:
            # Meta refuses; trying would only produce a confusing error.
            return False

        if not self.confirmed:
            # The seeded expiry is a guess. Replace it with fact as soon as
            # Meta will let us.
            return True

        return self.expires_at - now < REFRESH_THRESHOLD

    def to_json(self) -> str:
        payload = asdict(self)
        payload["expires_at"] = self.expires_at.isoformat()
        payload["refreshed_at"] = self.refreshed_at.isoformat() if self.refreshed_at else None
        payload["seeded_at"] = self.seeded_at.isoformat() if self.seeded_at else None
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "TokenInfo":
        payload = json.loads(raw)

        def parse(value):
            if not value:
                return None
            parsed = datetime.fromisoformat(value)
            # A naive datetime here would break every comparison downstream.
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        return cls(
            access_token=payload["access_token"],
            expires_at=parse(payload["expires_at"]),
            refreshed_at=parse(payload.get("refreshed_at")),
            seeded_at=parse(payload.get("seeded_at")),
            confirmed=bool(payload.get("confirmed", False)),
            bootstrap_fingerprint=payload.get("bootstrap_fingerprint", ""),
            ig_user_id=payload.get("ig_user_id", ""),
        )


class TokenStore:
    def __init__(self, storage, path: str = TOKEN_PATH):
        self.storage = storage
        self.path = path

    def load(self, bootstrap_token: str, ig_user_id: str, *, now: datetime | None = None) -> TokenInfo:
        """Return the live token, seeding from the bootstrap if needed.

        Never writes. The pipeline calls this first, then decides separately
        whether to refresh — so a dry run can inspect the token without
        changing anything.
        """
        now = now or _now()
        fingerprint = _fingerprint(bootstrap_token)

        try:
            stored = TokenInfo.from_json(self.storage.read(self.path).decode("utf-8"))
        except FileNotFoundError:
            log.info("No stored token; seeding from the bootstrap secret.")
            return self._seed(bootstrap_token, ig_user_id, fingerprint, now)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # Better to start over from the bootstrap than to guess at a
            # half-written file.
            log.warning("Stored token is unreadable (%s); reseeding.", exc)
            return self._seed(bootstrap_token, ig_user_id, fingerprint, now)

        if stored.bootstrap_fingerprint and stored.bootstrap_fingerprint != fingerprint:
            # The user replaced the secret, which is how someone recovers from
            # a dead token. Honour the new one.
            log.info("The bootstrap secret changed; reseeding from it.")
            return self._seed(bootstrap_token, ig_user_id, fingerprint, now)

        if stored.ig_user_id and ig_user_id and stored.ig_user_id != ig_user_id:
            log.info("The configured account changed; reseeding.")
            return self._seed(bootstrap_token, ig_user_id, fingerprint, now)

        return stored

    def _seed(self, token: str, ig_user_id: str, fingerprint: str, now: datetime) -> TokenInfo:
        return TokenInfo(
            access_token=token,
            expires_at=now + ASSUMED_LIFETIME,
            seeded_at=now,
            confirmed=False,
            bootstrap_fingerprint=fingerprint,
            ig_user_id=ig_user_id,
        )

    def save(self, info: TokenInfo) -> None:
        self.storage.write(self.path, info.to_json().encode("utf-8"))

    def refresh_if_needed(
        self, info: TokenInfo, client, *, now: datetime | None = None
    ) -> tuple[TokenInfo, bool]:
        """Refresh the token when it is due. Returns the token and whether it changed.

        A refresh failure that is not an auth problem is logged rather than
        fatal — there are days of margin left, and failing the run would stop a
        post that could have gone out.
        """
        now = now or _now()

        if info.seeded_at and not info.confirmed:
            # load() deliberately never writes, so a seed that is only in
            # memory would reset its own age on every run and the 24-hour gate
            # would never open. Persist it the first time we are asked.
            try:
                self.storage.read(self.path)
            except FileNotFoundError:
                self.save(info)

        if not info.needs_refresh(now):
            return info, False

        try:
            new_token, expires_in = client.refresh_access_token()
        except InstagramError as exc:
            if exc.disposition is Disposition.AUTH:
                # Nothing can publish with a dead token.
                raise
            log.warning(
                "Could not refresh the token (%s). %.1f days remain; will retry.",
                exc,
                info.days_remaining(now),
            )
            return info, False

        refreshed = TokenInfo(
            access_token=new_token,
            expires_at=now + timedelta(seconds=expires_in),
            refreshed_at=now,
            seeded_at=info.seeded_at,
            confirmed=True,
            bootstrap_fingerprint=info.bootstrap_fingerprint,
            ig_user_id=info.ig_user_id,
        )

        # Persist before returning: a token in memory but not on disk would be
        # lost, and Meta keeps the previous one valid so a failed write here is
        # recoverable on the next run.
        self.save(refreshed)
        log.info("Token refreshed; valid for %.0f more days.", refreshed.days_remaining(now))
        return refreshed, True
