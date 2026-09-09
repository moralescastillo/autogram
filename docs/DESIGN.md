# Autogram — Design

Status: **implemented**. This document and the code are kept in step; where
implementation proved a decision wrong, the decision was changed here too.
Date: 2026-09-09

A public, forkable tool that publishes content to an Instagram Business or
Creator account on a schedule. The user's content and state live in their own
cloud storage; the repository holds only code. Scheduling runs on GitHub
Actions.

---

## 1. Design principles

These are the constraints every decision below is measured against.

1. **Nothing personal enters the repository.** Not content, not captions, not
   the publish ledger, not logs. A fork is pure code. The only user-specific
   data in GitHub is a small set of encrypted secrets.
2. **One string configures storage.** A single connection string names where
   content lives. Everything else the tool discovers or creates for itself.
3. **Phone and desktop are equal.** Every routine action — writing a caption,
   reordering a carousel, publishing immediately — is doable from a phone
   without touching GitHub.
4. **The content is final.** The tool posts what it is given. It does not
   watermark, resize, or crop. (One exception, forced by the API — see §5.2.)
5. **Fail loudly, never silently.** A broken post is visible where the user
   will trip over it.

---

## 2. What was verified, and what still needs checking

Research done 2026-09-09 against Meta's developer documentation and current
sources. Two findings changed the design materially.

### 2.1 Verified

| Fact | Consequence |
|---|---|
| **Instagram API with Instagram Login requires no linked Facebook Page** | Onboarding gets far simpler than the legacy setup. This is the path we use. |
| Publishing is two steps: `POST /media` → `POST /media_publish` | Unchanged from the legacy code. |
| Media must be at a **publicly fetchable URL**; Meta cURLs it | Storage must be able to serve media over HTTPS. No file upload exists. |
| Container status polls `GET /<container-id>?fields=status_code` → `FINISHED` / `IN_PROGRESS` / `ERROR` / `EXPIRED` / `PUBLISHED` | Replaces the legacy retry-on-error-subcode hack with a real state check. |
| **Images must be JPEG.** PNG, WebP, HEIC, and GIF are all rejected | Collides with principle 4. Resolved in §5.2. |
| Carousel: max 10 items; **all children are cropped to the first item's aspect ratio** | Makes the `aspect` validation genuinely load-bearing. |
| Feed aspect ratio must fall between 4:5 and 1.91:1 | Validation rule. |
| Reels: 9:16, H.264/HEVC, up to 100MB / 15 min | Validation rule. |
| Rate limit: 100 published posts per rolling 24h; carousel counts as one | Far above any realistic cadence. Not a design concern. |
| Scopes: `instagram_business_basic`, `instagram_business_content_publish` | Setup instructions. |
| **No App Review is needed to publish to your own account.** Meta's *Standard Access* is granted automatically and covers accounts holding a role on the app (Administrator, Developer, Tester); an app in Development mode may request permissions from those users at standard or advanced access level | Verified 2026-09-09 against Meta's App Modes, App Roles and Permissions docs. This is what makes plug-and-play real — see §10.1. |
| **Advanced Access** — needed to publish for accounts *without* a role on the app — requires App Review and Business Verification | Only relevant if someone runs Autogram as a hosted service for others. Out of scope (§11). |
| **Long-lived tokens expire in 60 days and must be refreshed** while still valid; a token unrefreshed for 60 days is dead permanently | Drives §4 entirely. Refresh requires the token be ≥24h old. |
| GitHub Actions disables scheduled workflows after 60 days of repository inactivity on public repos; **only commits reliably reset the clock** | Drives §7.4. |
| GCS supports HMAC keys usable with S3-compatible clients against `storage.googleapis.com` | Keeps GCS config to one string, no service-account JSON. |
| **Google Drive service accounts have zero storage quota** and cannot upload, even into a folder you own | Drive must use OAuth with a refresh token — which is one string, not a JSON blob. This reverses an earlier assumption in this design. |
| A Google OAuth app left in "testing" status expires refresh tokens after **7 days** | The app must be set to "in production". Must be prominent in setup docs. |
| **Drive needs the full `drive` scope, not `drive.file`.** `drive.file` only covers files the app itself created or the user picked through Google's Picker UI — folders made by hand in the Drive mobile app are invisible to it | Verified 2026-09-09. Since authoring by hand on a phone *is* the workflow, the narrow scope cannot work. See §6.2.1. |
| `drive` is a **restricted** scope: an unverified app using it is capped at 100 users lifetime, and verification would require a CASA security assessment | Harmless here — each user runs their own app for themselves, so the cap is never approached. It does mean each user clicks through an "unverified app" warning once. Google names personal use as an explicit exception to verification. |

### 2.2 To verify during implementation

Listed so they are not silently assumed:

- **Current Graph API version.** Meta's docs show `v25.0`, which is the
  default; it is configurable, so a bump is a one-line change. Worth
  confirming against Meta's changelog before the first live run.
- **Signed URL compatibility.** Whether Meta's fetcher accepts GCS V4 presigned
  URLs generated via HMAC, and whether the `GoogleAccessId` parameter swap is
  needed. Fallback: temporarily public objects, deleted after publish.
- **Whether `gh workflow enable` resets the inactivity clock**, which would
  avoid keepalive commits entirely.

---

## 3. Architecture

```
┌────────────────────┐         ┌──────────────────────┐
┌─────────────────────┐        ┌──────────────────────┐
│  AUTHORING          │        │  User's fork (public)│
│  Google Drive       │        │  code only, no data  │
│                     │        │                      │
│   queue/            │◄───────┤  GitHub Actions      │
│   now/              │ reads  │  hourly cron         │
│   published/        │ writes │                      │
│   state/            │        │  Secrets:            │
│     token.json      │        │   AUTOGRAM_STORAGE   │
│     published.json  │        │   AUTOGRAM_IG_TOKEN  │
│     log/            │        │   AUTOGRAM_IG_USER_ID│
└──────────┬──────────┘        └──────────────────────┘
           │ media staged for publish
           ▼
┌─────────────────────┐
│  SERVING            │
│  GCS (private)      │
└──────────┬──────────┘
           │ signed URL
           ▼
   ┌──────────────┐
   │  Instagram   │  fetches media, publishes
   │  Graph API   │
   └──────────────┘
```

The Action is stateless. All state lives in the user's storage. This is what
makes the fork disposable and keeps the repository clean.

**The order of a run**, which carries most of the safety properties:

1. Load config, storage and the posting policy.
2. Load the token; refresh it when due. A dead token stops everything.
3. **Resolve any unfinished publish** (§8.1.1) — before scheduling, because
   recovering a published post writes the ledger, and the ledger is what tells
   the scheduler whether today's slot is already used.
4. Publish anything in `now/`, ignoring cadence.
5. Otherwise ask the scheduler whether a queued post is due, and publish one.

Within a publish, the pending marker is written **as soon as the final
container exists — before waiting on it**, not before publishing. The wait can
run fifteen minutes, which is exactly where a runner timeout lands; a marker
written after it would protect nothing.

---

## 4. Authentication and the token problem

This is the hardest constraint in the design, and the one the legacy system
never solved: nothing in it ever refreshed the access token, so posting broke
silently whenever the token lapsed.

**The problem.** Long-lived Instagram tokens expire after 60 days. Refreshing
returns a *new* token string. A GitHub Actions secret is the obvious place to
store a token, but a workflow's default `GITHUB_TOKEN` cannot write repository
secrets — so the Action cannot rotate its own secret without the user creating
an additional personal access token with elevated permissions. That is both a
security smell and an onboarding burden.

**The solution.** The token lives in storage, not in GitHub.

- `AUTOGRAM_IG_TOKEN` is a **bootstrap** secret, read only once.
- On every run, the tool reads `state/token.json` from storage. If absent, it
  seeds it from the bootstrap secret.
- If the token expires in **fewer than 7 days**, the tool refreshes it and
  writes the new token and `expires_at` back to `state/token.json`.
- Refresh requires the token be at least 24 hours old; the tool checks this.

```json
{
  "access_token": "IGQ...",
  "expires_at": "2026-11-08T12:00:00Z",
  "refreshed_at": "2026-09-09T12:00:00Z"
}
```

Because the workflow runs hourly, a 7-day window gives roughly 168 chances to
refresh before expiry. Any single failure is harmless.

**The unknown-age problem.** A bootstrap token's age cannot be read from the
token itself — it may have been minted a minute or seven weeks ago. So seeding
assumes 60 days and marks the record *unconfirmed*; once the token is old
enough for Meta to accept a refresh (24 hours), the next run refreshes it
regardless of how distant expiry looks, replacing the assumption with an exact
lifetime. The only case this loses is a bootstrap already near expiry, and it
loses loudly: the run fails and GitHub emails the user.

**Recovering from a dead token.** The stored record keeps a fingerprint of the
bootstrap secret it came from. Pasting a new secret into GitHub changes the
fingerprint, and the next run reseeds from it — so a user recovers by editing
one secret, never by hand-editing files in their Drive.

**Warning threshold.** If a token is within 7 days of expiry *and* refresh has
failed, that is logged as an error and written to `state/log/` — the user finds
out before a post is missed, not after.

**A note on the existing token.** The legacy `IG_LL_ACCESS_TOKEN` was issued
under the Facebook Login path (`graph.facebook.com`, with a linked Page). This
design uses Instagram Login (`graph.instagram.com`) — a different app type and
a different host. The old token will not work here. Setup means creating a
fresh Meta app; there is no migration path, and pretending otherwise would
waste time.

---

## 5. The content contract

### 5.1 One folder per post

The folder *is* the post. This is the central decision, and it exists to fix a
specific failure in the legacy system: captions lived in a Google Sheet and
were matched to media by name, so a rename or reorder silently paired the wrong
caption with the wrong image. Here, the caption lives inside the post. It
cannot desync, because there is nothing to match.

```
queue/
  2026-09-20-lisbon-run/
    post.md
    01.jpg
    02.jpg
    03.jpg
  2026-09-22-studio/
    post.md
    clip.mp4
now/
  urgent-announcement/
    post.md
    hero.jpg
published/
  2026-09-18-morning/
    ...
state/
  token.json
  published.json
  log/
    2026-09.jsonl
```

**Ordering** within a carousel is *natural* filename sort: `1.jpg`, `2.jpg`,
`10.jpg` — numbers read as numbers, so `10` comes last rather than second.
Renaming reorders. This works on any device, and zero-padded names
(`01`, `02`) sort correctly too.

**Queue order** is folder-name sort, which is why the date prefix convention is
suggested — but it is only a convention, not parsed for meaning. Unlike the
legacy system, **no metadata is encoded in filenames**. Encoding data there
made renaming dangerous: a file's name was parsed for meaning, so tidying up a
filename silently broke the pipeline. The only thing
a name controls is order.

### 5.2 `post.md`

YAML front matter plus caption body. Everything is optional.

```markdown
---
type: carousel        # single | carousel | reel | story
aspect: 1:1           # validation only — see below
user_tags:            # optional @-mentions in the media
  - username: somebody
    x: 0.5
    y: 0.5
---
Morning loop along the river. 12k, and it finally felt easy.

#running #lisbon #marathontraining
```

**A YAML trap, handled.** PyYAML implements YAML 1.1, where `1:1` is a
*sexagesimal* (base-60) integer, not a string — it parses as `61`, and `4:5`
as `245`. Since users will write ratios unquoted, both forms are accepted and
the base-60 encoding is inverted exactly. Quoting (`"1:1"`) also works. Without
this, every unquoted aspect ratio would silently validate against nonsense.

**Unrecognised keys are an error.** A typo like `aspcet:` would otherwise be
ignored in silence, and the user would never learn why their setting had no
effect.

**Defaults when omitted.** `type` is inferred: one image → `single`; multiple
images → `carousel`; one video → `reel`. A folder with no `post.md` at all is a
valid post with no caption. That is the plug-and-play floor — drop three photos
in a folder and it works.

**`aspect` is a check, not a transform.** The API has no aspect parameter;
Instagram reads the pixels. Declaring `aspect: 1:1` means the tool verifies
your images are actually 1:1 and **fails before posting** if one is not. This
matters most for carousels, where Instagram crops every child to match the
first item — a mismatched image would be silently mangled. Omit the field and
no check runs.

**Hashtags** are just caption text; nothing special is needed. **User tags**
are a real API field and are handled separately, as above.

### 5.3 The one permitted transform

Principle 4 says the tool does not process images. The API's JPEG-only rule
forces exactly one exception, because phones produce HEIC and screenshots
produce PNG — content that is otherwise final and correct.

**The tool converts non-JPEG images to JPEG before publishing.** It does not
resize, crop, or recompress beyond that. The original in storage is untouched;
conversion happens on a copy in transit. This is mandatory rather than
cosmetic: without it, the most common phone content simply cannot be posted.

A JPEG that is already correctly oriented passes through byte-for-byte, so the
common case loses nothing to recompression. Images needing rotation have it
baked into the pixels and the EXIF tag dropped, rather than trusting Instagram
to honour it. Transparency is flattened onto white, since JPEG has no alpha.

Videos are not transcoded. A video that does not meet Instagram's requirements
fails validation with a clear message.

### 5.4 Validation, before anything is uploaded

Every check runs up front, so failures cost nothing and are explained
precisely:

- Carousel has 2–10 items
- All carousel children share one aspect ratio (Instagram crops to the first
  otherwise)
- Feed aspect ratio between 4:5 and 1.91:1 — **stories are exempt**, since 9:16
  is correct for them and would fail the feed bounds
- Declared `type` matches the media present: a reel with no video, a story with
  two files, a `single` with three images
- Reel duration 3s–15min; story video ≤60s
- Images ≤8MB after conversion, videos ≤100MB
- Declared `aspect`, if present, matches the actual pixels (1% tolerance, so
  1080×1081 counts as square)
- Caption ≤2,200 characters, ≤30 hashtags
- Media files are of a recognized type

Ratios are measured **after** applying EXIF orientation. Phone cameras commonly
store landscape pixels plus a "rotate 90°" tag, so raw dimensions would see a
4:3 landscape where the user sees a 3:4 portrait and validate the wrong ratio
entirely.

Video checks depend on `ffprobe`. Where it is unavailable the video still
uploads and only the dimension and duration checks are skipped, with a warning
— Instagram remains the final authority, and these checks exist to fail faster
and more legibly, not to be the only gate.

**Every problem is reported at once.** Stopping at the first would have the
user fix, wait an hour, and discover the next one.

---

## 6. Storage

### 6.1 Two slots, not one

Storage fills two distinct roles, and conflating them caused confusion earlier
in this design:

| Slot | Job | Requirement |
|---|---|---|
| **Authoring** | Where you arrange photos and write captions, from any device | A good mobile app |
| **Serving** | Where Instagram fetches media over HTTPS | Public or signed URLs |

One backend can fill both, or two can split the work. The interface is the same
either way: list, read, write, move, and produce-a-fetchable-URL.

### 6.2 The recommended pairing: Google Drive + GCS

**Google Drive for authoring.** Good apps on desktop and mobile, and where the
user in question already works. Auth is OAuth with a refresh token — **one
string, no JSON blob.**

An earlier draft of this design claimed Drive required a service-account JSON
and recommended Dropbox on that basis. That was wrong twice over: service
accounts have **zero storage quota** and cannot upload to Drive at all, and the
correct OAuth path costs no more configuration than Dropbox does. The objection
that separated the two backends does not exist.

**GCS for serving.** HMAC keys give an access-key/secret pair usable with
S3-compatible clients — again one string, no JSON blob. Media is served through
signed URLs, so the bucket stays private; this matters because the token and
ledger live there too.

Pairing them keeps everything within one cloud provider, which is worth
something for account management even though the two services authenticate
separately.

**The Drive trap to document loudly:** an OAuth app left in *testing* status
expires refresh tokens after 7 days. It must be set to *in production* — a
status toggle, not a review process, for an app requesting only personal
scopes. This is the most likely setup mistake and belongs in bold in `SETUP.md`.

### 6.2.1 Alternatives, supported but not default

- **Dropbox** for authoring — marginally simpler token generation, equally good
  apps. A legitimate choice; the tool supports it. If
  `files/get_temporary_link` proves acceptable to Meta's fetcher, Dropbox can
  fill both slots alone and needs no object storage at all.
- **S3 or any S3-compatible store** for serving — the GCS implementation is an
  S3-compatible client pointed at a different endpoint, so this comes nearly
  free.
- **GCS for both slots** — viable, but GCS has no good phone client, so
  authoring means either clunky mobile access or a sync step. Only worth it for
  someone who does not need the mobile half.

### 6.2.2 The connection string

One secret, `AUTOGRAM_STORAGE`, holds one or two connection strings:

```
gdrive://<client_id>:<client_secret>:<refresh_token>@<folder_id>
gs://<access_key>:<secret>@<bucket>/<prefix>
dropbox://<refresh_token>@<root_path>
s3://<access_key>:<secret>@<bucket>/<prefix>
```

When both an authoring and a serving backend are configured, the tool reads
content from the first and stages media through the second. When only one is
given, it fills both roles. This is the "one string" promise — with a second
string only when the user deliberately splits the roles.

### 6.3 Serving media to Instagram

Meta must fetch media over HTTPS. No separate staging host is needed — the
serving backend *is* the staging host.

With the recommended pairing, publishing a post copies its media from Drive to
GCS, generates a signed URL, hands it to Instagram, and deletes the staged copy
once the post is live. Drive is never exposed publicly.

Preference order for the URL: a short-lived **signed URL** (bucket stays
private); failing that, a temporarily public object deleted immediately after
publish. Which one is in use gets stated plainly once §2.2 verification is
done.

**Why Drive cannot serve directly.** Drive share links return an HTML viewer
page, not raw bytes. Undocumented direct-download URL shapes exist and
sometimes work for images, but they are unreliable and generally fail for
video. Depending on them would be the kind of thing that breaks silently in two
years, which is exactly what this design is trying to avoid.

---

## 7. Scheduling

### 7.1 Policy

`autogram.yml`, at the storage root — configuration lives with content, not in
the repo:

```yaml
timezone: Europe/Lisbon

schedule:
  cadence: every 2 days      # or: monday,thursday | daily
  window: "06:00-21:00"      # random time within this window
```

**Grammar.** `cadence` is `daily`, `every N days` (N ≥ 1), or a comma-separated
list of weekdays (full names or three-letter abbreviations, any case).
`window` is `HH:MM-HH:MM` and must start before it ends — windows spanning
midnight are not supported, since they make "which day is this slot on?"
ambiguous. `timezone` is any IANA name.

**Defaults**, when `autogram.yml` is absent or incomplete: post `daily`,
between `09:00-21:00`, in `UTC`.

**The interval anchor is the last publish**, not a fixed epoch. "Every 2 days"
means two days since the last post, so the first post is due immediately and a
missed day does not permanently shift the rhythm. Weekday cadences need no
anchor.

At most one post per eligible day.

### 7.2 Randomized posting times

The workflow runs **hourly**, and each run decides for itself whether a post is
due.

The day's target time is derived from the date and the account id with SHA-256,
mapped into the configured window. It is therefore stable — every run on a
given day computes the same target without storing it — while still looking
unpredictable from outside. Including the account id keeps two accounts on
identical schedules from posting in lockstep. (Python's built-in `hash()` will
not do: string hashing is randomised per process, so two runs on the same day
would disagree.)

**Why a run cannot simply check "is it the target hour?"** GitHub's scheduler
fires late under load — by minutes, sometimes by an hour — and occasionally
skips a tick entirely. An equality check would miss the slot and lose the post
for the whole cycle. So the test has three parts:

1. today is an eligible day, **and**
2. the local clock is at or past today's target, **and**
3. nothing has been published today yet

The third part is why this needs the ledger's last-publish timestamp, and it is
what makes lateness harmless: a run at 15:03 for a 14:37 target still publishes,
and the run after it does not. An earlier draft of this design claimed no state
was needed; that was wrong, and drift is the reason.

All three are evaluated in the user's **local** date. A 23:30 UTC run is
already tomorrow in Tokyo, and judging by the UTC date would skip a post that
is genuinely due.

### 7.3 Immediate posting

Anything in `now/` is published on the next hourly run, ignoring cadence
entirely. Drop a folder there from a phone; no GitHub interaction. Published
posts move to `published/` like any other.

### 7.4 Two Actions caveats

**Drift.** Scheduled workflows fire late under load — minutes to an hour. Times
are approximate by nature. (Accepted; see conversation.)

**The 60-day inactivity rule.** Scheduled workflows on public repos are
disabled after 60 days without repository activity, and only commits reliably
reset the clock. The tool therefore includes a keepalive that commits a trivial
marker before the deadline.

This appears to violate principle 1, so to be explicit: the marker contains **a
timestamp and nothing else** — no content, no captions, no evidence of what was
posted or when. It is a heartbeat, not a record. If `gh workflow enable` turns
out to reset the clock (§2.2), even that disappears.

### 7.5 Concurrency

```yaml
concurrency:
  group: autogram-publish
  cancel-in-progress: false
```

Video processing can exceed an hour, overlapping the next tick. Combined with
the ledger check (§8.1) before each publish, this makes double-posting
structurally impossible — a real improvement on the legacy Sheets approach,
which used a spreadsheet as a queue and read, acted on, then deleted a row
with no locking — fine for one nightly job, unsafe for anything concurrent.

---

## 8. State and reporting

### 8.1 The ledger

`state/published.json`, in storage. Records what was published and when.
Checked before every publish, which is what makes runs idempotent and retries
safe.

### 8.1.1 The crash window

The ledger stops a *completed* publish being repeated. It does not by itself
stop a duplicate when a run dies between Instagram accepting the post and the
ledger being written — a real possibility on a runner that hits its timeout
mid-publish.

The container id closes that window. Before calling ``media_publish`` the
pipeline records the container id; if a later run finds one recorded, it asks
Instagram what became of it:

| Status | Meaning | Action |
|---|---|---|
| `PUBLISHED` | It went out; only the bookkeeping was lost | Record it, retire the post, do **not** publish again |
| `FINISHED` | Ready but never published | Publish it |
| `ERROR` / `EXPIRED` | Never going to work | Fail the post, start over |

This is what `status_code` is for, and it is why the client returns container
ids from every create call and takes one in `publish` rather than hiding the
two-step handshake behind a single method.

### 8.2 Logs

`state/log/YYYY-MM.jsonl` — one JSON object per **event**. Machine-readable by
design, so a separate tool can pick it up for email reporting without this
codebase knowing anything about email. (The legacy system embedded SMTP
credentials in source, and they leaked. This design keeps notification out of
the tool entirely.)

Events, not runs: the workflow runs hourly, so a record per run would be 700+
"nothing to do" lines a month burying the handful that matter. Only things that
happened are recorded — `published`, `failed`, `retry_later`, `recovered`,
`token_refreshed`, `token_warning`. The per-run trace already exists free in
the Actions log.

```json
{"ts":"2026-09-20T14:00:00Z","event":"published","post":"2026-09-20-lisbon-run","type":"carousel","media_id":"178..."}
{"ts":"2026-09-21T09:00:00Z","event":"error","post":"2026-09-22-studio","reason":"reel_too_long","detail":"video is 18m, limit 15m"}
```

### 8.3 Failure handling

Three layers, all zero-configuration:

1. **The Actions run fails** — GitHub emails the user by default.
2. **`error.txt` is written into the post folder** — the user sees it in the
   storage app, next to the content that needs fixing, with a plain-language
   explanation. A failed post stays in `queue/` and is retried; it is not
   silently skipped.
3. **The structured log** captures it for downstream reporting.

**Not doing:** opening GitHub issues (the repo is public — issue titles would
leak posting activity), or sending email directly (credentials in the
publishing path is the exact mistake the legacy system made).

### 8.4 Webhook seam

An interface with a no-op default. When a webhook URL is configured, run
outcomes POST to it; with none, nothing happens and nothing is imported. Not
implemented in v1 — the seam exists so adding it later touches one file.

### 8.5 Container polling

Meta suggests polling once a minute for up to five minutes. Large reels
routinely take longer, so the ceiling is **15 minutes with backoff**. `ERROR`
and `EXPIRED` are terminal and route to §8.3 immediately rather than burning
the full timeout.

---

## 9. Repository layout

```
autogram/
├── .github/workflows/
│   ├── publish.yml           # hourly cron + manual dispatch
│   └── keepalive.yml         # monthly marker commit
├── src/autogram/
│   ├── instagram/            # one Graph client, one API version
│   ├── storage/              # base + gdrive + gcs + dropbox + s3
│   ├── content/              # post discovery, post.md parsing, validation
│   ├── scheduling/           # cadence + deterministic time selection
│   └── state/                # token, ledger, log
├── tests/
├── docs/
│   ├── DESIGN.md             # this file
│   └── SETUP.md              # the onboarding walkthrough
└── README.md
```

Python, matching the legacy code and keeping the door open for the watermark
project as a separate upstream step.

Note on the legacy code: it accumulated two separate Graph clients pinned to
two different API versions, which drifted apart. This design has exactly one
client and one configurable version.

---

## 10. Onboarding

### 10.1 Why no App Review is needed — the key finding

The plug-and-play claim rests entirely on this, so it is worth stating
precisely. Verified 2026-09-09 against Meta's own documentation.

Meta grants every app **Standard Access** automatically. Standard Access covers
**only accounts that hold a role on the app** — Administrator, Developer, or
Tester. An app in Development mode "can request permissions from role users, and
only permissions with standard or advanced access levels," and Administrators,
Developers and Testers "can grant the app any permission while it is in
development."

The consequence, and it is a good one:

> **Each user creates their own Meta app, holds the Administrator role on it by
> definition, and publishes to their own Instagram account with no App Review
> at all.**

Because every user runs their own app against their own account, nobody ever
needs **Advanced Access** — which is what requires App Review, Business
Verification, and a 2–4 week wait. This is a direct consequence of the
fork-per-user architecture: there is no shared app, so there is no shared
review.

**The boundary, so nobody trips over it:** Advanced Access becomes necessary
only if an app publishes for accounts that hold no role on it — that is, if
somebody ran Autogram as a hosted service for other people. That is explicitly
out of scope (§11). Forking is what keeps every user on the free side of this
line.

**Known constraints on Standard Access:** rate limits are tighter than Advanced
Access. Meta's publishing limit of 100 posts per rolling 24 hours is far above
any realistic cadence, so this does not bind in practice — but a user posting
at industrial volume would notice.

### 10.2 The steps


The honest version of "plug and play". Steps 1–3 are the real cost, and no
design can remove them — they are Meta's and Google's requirements, not this
project's.

1. Create a Meta app and add the Instagram product. Leave it in **Development
   mode** — you are its Administrator, so no App Review is needed (§10.1)
2. Generate a long-lived token with `instagram_business_basic` and
   `instagram_business_content_publish`
3. Create storage: a Google Drive folder plus an OAuth refresh token, and a
   GCS bucket with an HMAC key. **Set the Google OAuth app to "in production"**
   or the refresh token dies after 7 days (§6.2)
4. Fork the repository
5. Add three secrets: `AUTOGRAM_STORAGE`, `AUTOGRAM_IG_TOKEN`,
   `AUTOGRAM_IG_USER_ID`
6. Drop a folder of photos into `queue/`

Realistically 15–20 minutes, nearly all of it in Meta's developer console.
After that, the user never touches GitHub again — all ongoing work happens in
their storage app, on whatever device they have.

---

## 11. Deliberately out of scope

- **Watermarking and Garmin integration.** A separate project. If it happens,
  it writes finished content into `queue/` and this tool never knows.
- **A web UI.** Breaks the cheap and plug-and-play constraints.
- **Multi-account.** One account per fork for v1. The config shape (a list, not
  a scalar) leaves room without committing to it.
- **Analytics and engagement.** This tool posts. Nothing more.
- **The Facebook Login path.** Instagram Login is simpler and needs no Page.
  Supporting both would double the auth surface for no gain.

---

## 12. Open questions

1. **Should the tool create the folder structure on Drive, or expect the user
   to?** Depends on the `drive.file` scope question in §2.2. Tool-created is
   better for plug-and-play and for keeping the OAuth scope narrow.
2. ~~Should a failed post block the queue or be skipped?~~ **Resolved:
   skipped.** A broken post keeps its place in the folder and is retried, but
   it does not stop the posts behind it — one typo silently halting a feed is
   a worse failure than a post going out of order. The user learns about it
   from an `error.txt` and a failed run.
3. **How long should `published/` retain content** before archival or deletion?
   Currently forever.
4. **Repository name.** `autogram` is the working directory name.
