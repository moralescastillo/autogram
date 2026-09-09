# Setup

Roughly twenty minutes, most of it in Meta's developer console. Steps 1–3 are
the real cost and no tool can remove them — they are Meta's and Google's
requirements, not this project's.

> **Status:** skeleton. Exact console click-paths are filled in once the
> implementation is verified against the live APIs.

---

## Before you start

- An Instagram **Business or Creator** account. Personal accounts cannot use
  the publishing API, and there is no way around this.
- A Google account (for Drive and, if you use it, GCS).
- A GitHub account.

---

## 1. Create a Meta app

Instagram's newer "Instagram API with Instagram Login" needs **no Facebook
Page** — a real simplification over the older path.

1. Go to <https://developers.facebook.com/apps> and create an app
2. Add the **Instagram** product
3. Connect your Instagram professional account
4. **Leave the app in Development mode.** Do not submit it for App Review.

### You do not need App Review

This surprises people, so to be clear about why.

Meta gives every app *Standard Access* automatically, and Standard Access
covers any account holding a role on the app. You created the app, so you are
its Administrator — which means you can publish to your own Instagram account
straight away, with no review and no waiting.

App Review and Business Verification are for publishing on behalf of *other
people's* accounts. Because you run your own app against your own account, that
never applies. It is the reason this project asks you to make your own app
rather than sharing one: the two-to-four week review queue is skipped entirely.

The one practical limit: Instagram allows 100 published posts per rolling 24
hours. A carousel counts as one.

## 2. Generate a long-lived token

Request these scopes:

- `instagram_business_basic`
- `instagram_business_content_publish`

Exchange the short-lived token for a long-lived one. Keep it — it goes into
GitHub in step 5.

**You do not need to renew this by hand.** Long-lived tokens expire after 60
days, and Autogram refreshes yours automatically well before then, storing the
current one in your storage rather than in GitHub.

You also need your **Instagram user ID** (a long number, not your @handle).

## 3. Set up storage

### Google Drive — where you will work

1. Create a folder in Drive for Autogram. Its **folder ID** is the last part of
   the URL when you open it: `drive.google.com/drive/folders/<folder_id>`
2. In Google Cloud Console, create an **OAuth client ID** of type *Desktop app*.
   Keep the **client ID** and **client secret**
3. Run the auth command, which handles the rest:

```bash
pip install -e .

autogram auth gdrive \
  --client-id     <your client id> \
  --client-secret <your client secret> \
  --folder-id     <your folder id>
```

Your browser opens, you approve access, and the command prints a connection
string ready to paste into GitHub. It checks the connection before printing, so
if something is wrong you find out now rather than at 3am.

On a machine with no browser, add `--no-browser` and open the printed URL
somewhere else.

**You will see an "unverified app" warning.** That is expected and correct: the
app is yours, used only by you, and Google names personal use as an exception
to its verification requirements. Click *Advanced* → *Go to (your app)* to
continue.

> ### ⚠️ Set the OAuth app to "In production"
>
> An app left in **Testing** status expires refresh tokens after **7 days**,
> and your posting will stop dead about a week after setup.
>
> In Google Cloud Console → *APIs & Services* → *OAuth consent screen*, click
> **Publish app**.
>
> You do **not** need to submit for verification. Publishing is a status
> change; verification is a separate process you can ignore while you are the
> only user. Your app stays capped at 100 users, which is 99 more than you
> need.
>
> This is the single most common way to get this setup wrong.

### An object store — where Instagram fetches from

Instagram's API does not accept uploads; it fetches media from a URL. Drive
cannot provide one (its links return a viewer page, not raw bytes), so you need
an object store. Your media is copied there only while a post is being
published, then deleted.

**Google Cloud Storage:**

1. Create a bucket — keep it **private**
2. Create an **HMAC key** (*Cloud Storage* → *Settings* → *Interoperability*).
   This gives an access key and secret, so no service-account JSON is involved.

**Amazon S3** works identically; use an access key and secret with read/write
on the bucket.

## 4. Fork this repository

Use the **Fork** button. Keep it public — GitHub Actions minutes are free on
public repositories, which is what makes this cost nothing to run.

Your content never enters the repository, so a public fork exposes nothing
about what you post.

## 5. Add your secrets

In your fork: *Settings* → *Secrets and variables* → *Actions* → **New
repository secret**.

| Secret | Value |
|---|---|
| `AUTOGRAM_STORAGE` | Your connection strings, one per line (below) |
| `AUTOGRAM_IG_TOKEN` | The long-lived token from step 2 |
| `AUTOGRAM_IG_USER_ID` | Your Instagram user ID from step 2 |

`AUTOGRAM_STORAGE` takes the authoring backend on the first line — the string
`autogram auth gdrive` printed — and the serving backend on the second:

```
gdrive://<client_id>:<client_secret>:<refresh_token>@<folder_id>
gs://<access_key>:<secret>@<bucket>/autogram
```

If you use one backend that can do both, a single line is enough:

```
gs://<access_key>:<secret>@<bucket>/autogram
```

## 6. Configure your schedule

Create `autogram.yml` in the root of your Drive folder:

```yaml
timezone: Europe/Lisbon

schedule:
  cadence: every 2 days      # or: monday,thursday | daily
  window: "06:00-21:00"      # posts land at a random time in here
```

## 7. Make your first post

In your Drive folder:

```
queue/
  2026-09-20-first-post/
    post.md
    01.jpg
```

`post.md` — every field is optional:

```markdown
---
type: single
---
My first automated post.

#hello
```

A folder with no `post.md` at all is still a valid post; it just has no
caption.

### Before you wait for the schedule

Check your storage connection any time with:

```bash
autogram auth verify "<your connection string>"
```

Then run the workflow by hand with **dry run** ticked: *Actions* → *publish* →
*Run workflow*. It reports exactly what it would do without posting anything.

---

## Things worth knowing

**Images must be JPEG.** Instagram rejects PNG, WebP and HEIC. Autogram
converts them for you on the way out and leaves your originals untouched — the
only change it ever makes to your content.

**Carousels crop to the first image.** Every image in a carousel is cropped to
match the aspect ratio of the first one. If you want a specific ratio, set
`aspect: 1:1` in `post.md` and Autogram will check your images actually match
before posting, rather than letting Instagram silently crop them.

**Posting immediately.** Anything in `now/` goes out on the next hourly run,
ignoring your schedule. It works from your phone with no GitHub involvement.

**When something fails.** An `error.txt` appears in the post's folder
explaining what went wrong, in plain language. GitHub also emails you about the
failed run. The post stays in `queue/` and is retried, so nothing is lost.

**Timing is approximate.** GitHub's scheduler drifts, sometimes by up to an
hour under load. Combined with the random window, treat posting times as
roughly right rather than exact.

**Rate limit.** Instagram allows 100 published posts per 24 hours. A carousel
counts as one. You are unlikely to come near this.
