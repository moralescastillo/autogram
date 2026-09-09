# Autogram

Schedule and publish content to an Instagram Business or Creator account from
your own cloud storage, using GitHub Actions as the scheduler.

**Status: complete, awaiting its first live run.** Every component is built and
tested against real storage and a real S3 implementation, with Instagram's HTTP
faked. Nothing has yet published to a live account — see
[`docs/SETUP.md`](docs/SETUP.md) to be the first.

## What it does

Put a folder of photos in your Drive. Write a caption in a text file next to
them. The post goes out on your schedule — every two days, every Thursday,
whatever you set — at a randomised time inside a window you choose, so it does
not look like a machine posting on the hour.

Everything you do day to day happens in your storage app, on your phone or your
desktop. After setup you never open GitHub again.

## How it is put together

- **Your content lives in your storage**, never in this repository. Fork it and
  it stays pure code — no captions, no images, no record of what you posted.
- **A folder is a post.** The caption lives inside the folder it belongs to, so
  it cannot drift onto the wrong picture.
- **Scheduling runs on GitHub Actions**, which is free on public repositories.
- **Configuration is three secrets**, one of which is a single connection
  string naming where your content lives.

Supports single images, carousels, reels and stories.

## Getting started

Fork this repository, then follow [`docs/SETUP.md`](docs/SETUP.md). Budget about
twenty minutes, nearly all of it in Meta's developer console.

**You do not need Meta App Review.** Because you run your own app against your
own account, you are its administrator and can publish immediately — the two-to-
four week review queue only applies to apps posting on behalf of other people.

You will need:

- An Instagram Business or Creator account (personal accounts cannot use the
  publishing API)
- A Meta developer app — free, and no Facebook Page is required
- Somewhere to keep content: Google Drive for authoring, plus an object store
  (GCS or S3) that Instagram can fetch media from

## Why it works this way

Instagram's publishing API does not accept file uploads. It takes a URL and
fetches the media itself, which is why an object store sits in the pipeline
even though Drive is where you actually work. Drive share links return a viewer
page rather than raw bytes, so they cannot be used directly.

The reasoning behind every other decision — the token refresh, the folder
layout, the randomised scheduling, what happens when a post fails — is in
[`docs/DESIGN.md`](docs/DESIGN.md).

## License

MIT. See [`LICENSE`](LICENSE).
