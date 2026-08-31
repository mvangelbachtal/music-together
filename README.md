# Music Together

Music Together is a Dockerized party music voting player. The host creates a session and guests can submit YouTube or YouTube Music URLs and upvote each active queue item once.

This repository contains the runnable application, deployment configuration, and regression tests.

## Run

Create the local environment file first:

```sh
cp .env.example .env
```

Edit `.env` with the values for the integrations you want to use, then start the published image:

```sh
docker compose up
```

For local development from the current source tree, use the development Compose file:

```sh
docker compose -f compose.dev.yml up --build
```

The default image is `ghcr.io/mvangelbachtal/music-together:latest`. The CI also publishes `main`, release tags, and commit SHA tags. Set `IMAGE_TAG` in `.env` to use another published tag.

Pushing a `v*` tag builds the matching image and creates a GitHub Release. To backfill an existing tag, run the `Build and publish Docker image` workflow manually and provide that tag in the `release_tag` input.

Open `http://localhost:8000`. The application stores its SQLite database in `data/music-together.db` next to the Compose file. The `data/` directory is ignored by git so the database remains local to the deployment.

## Development

Install uv, then synchronize the development environment with `uv sync --dev`. Install the hooks with `uv run pre-commit install --hook-type pre-commit --hook-type commit-msg`. Run all hooks with `uv run pre-commit run --all-files`.

Commits use the Conventional Commits format. Commitizen is configured with initial version `0.0.0`, so the first feature release will become `0.1.0`. It updates `pyproject.toml` and generates `CHANGELOG.md` during a future `cz bump`. Version bumping is intentionally not run automatically.

## Environment

Compose reads these values from `.env` automatically:

| Variable | Required | Description |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | Yes | Public base URL used to generate host, guest, kiosk, and QR links. Use the HTTPS URL when deployed behind nginx. |
| `YOUTUBE_API_KEY` | Search only | YouTube Data API v3 key. Without it, guests can still add songs by pasting YouTube URLs. |
| `SPONSORBLOCK_ENABLED` | No | Set to `0` to disable automatic community-marked intro/outro skipping. |
| `SPONSORBLOCK_CATEGORIES` | No | Comma-separated categories to skip. Defaults to sponsor, self-promo, interaction, intro, outro, preview, filler, and music-off-topic. |
| `OIDC_ISSUER` | OAuth only | Base URL of the OIDC provider, for example `https://auth.example.com`. |
| `OIDC_CLIENT_ID` | OAuth only | OIDC application client ID. |
| `OIDC_CLIENT_SECRET` | OAuth only | OIDC application client secret. Keep this private. |
| `OIDC_AUTHORIZATION_URL` | OAuth only | Provider authorization endpoint. |
| `OIDC_TOKEN_URL` | OAuth only | Provider token endpoint. |
| `OIDC_USERINFO_URL` | OAuth only | Provider user-info endpoint. |
| `OIDC_REDIRECT_URI` | OAuth only | Must exactly match the callback registered in the OIDC provider. |
| `OIDC_SCOPES` | No | Requested scopes; defaults to `openid email profile`. |
| `REQUIRE_OIDC_AUTH` | No | Set to `1` to require OIDC login before a host can create a session. |
| `PLAYBACK_OWNER` | No | Default browser that supplies playback for new sessions: `host` (default) or `kiosk`. The session creation toggle can override this per session. |

The application starts with empty optional integration values. To enable search, set `YOUTUBE_API_KEY`. SponsorBlock skipping is enabled by default and can be disabled with `SPONSORBLOCK_ENABLED=0`. To enable and require OIDC host login, set the OIDC variables in `.env` and set `REQUIRE_OIDC_AUTH=1`.

For kiosk-owned playback, enable the kiosk playback toggle when creating a session, or set `PLAYBACK_OWNER=kiosk` to make that the default. Open the kiosk URL on the device connected to the speakers and keep that page visible. Host play, pause, resume, stop, volume, and skip actions are synchronized to the kiosk player. Click anywhere on the kiosk once to satisfy the browser's audible-autoplay policy; no separate audio button is required.

### OIDC provider setup

For Authentik, create an OAuth2/OpenID Provider and an Application in the Authentik admin interface. A Terraform resource for this application is provided in `/home/jan/tofu/authentik-mva/applications/music-together.tf`. Other OIDC providers use equivalent settings.

#### Create `YOUTUBE_API_KEY`

This key enables the guest search box. It is not needed when guests only paste YouTube URLs.

1. Open [API credentials](https://console.cloud.google.com/apis/credentials) and select the project used by this application.
2. Open [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com) and click **Enable**.
3. Return to **APIs & Services > Credentials**, click **Create credentials**, then choose **API key**.
4. Copy the generated key into `.env`:

	```dotenv
	YOUTUBE_API_KEY=your_api_key_here
	```

5. For a public deployment, restrict the key on the credentials page to **YouTube Data API v3**. Never commit the real key to the repository.

#### Create OIDC client credentials

These credentials enable host sign-in. They are separate from `YOUTUBE_API_KEY`.

1. In your OIDC provider, create an OAuth2/OIDC application.
2. Under **Redirect URIs**, add this exact local URI:

	```text
	http://localhost:8000/auth/callback
	```

3. Copy the client ID and client secret into `.env`:

	```dotenv
	OIDC_ISSUER=https://auth.example.com
	OIDC_CLIENT_ID=your_client_id_here
	OIDC_CLIENT_SECRET=your_client_secret_here
	OIDC_AUTHORIZATION_URL=https://auth.example.com/application/o/authorize/
	OIDC_TOKEN_URL=https://auth.example.com/application/o/token/
	OIDC_USERINFO_URL=https://auth.example.com/application/o/userinfo/
	OIDC_REDIRECT_URI=http://localhost:8000/auth/callback
	REQUIRE_OIDC_AUTH=1
	```

For production, register the exact HTTPS callback URL used by the public deployment, for example `https://music.example.com/auth/callback`, then set the same value in `OIDC_REDIRECT_URI` and set `PUBLIC_BASE_URL` to `https://music.example.com`. Do not use a trailing slash unless that exact URL is registered in the provider.

## Current slice

- Persistent party sessions with separate guest and kiosk capability URLs
- Anonymous guest identity through an HTTP-only cookie
- YouTube and YouTube Music URL normalization by video ID
- Duplicate active submissions merge into one queue item
- One upvote per guest per active item, with a second click removing the vote
- Individually requested songs are auto-voted by the requester, ranking above bulk-imported playlist items
- Bulk playlist import adds every video from a YouTube playlist to the queue with zero votes so guests can still vote them up
- Deterministic vote ranking with submission-time tie-breaking
- Kiosk read endpoint and QR code endpoint
- Host playback-state controls
- Ranked queue auto-advancement
- Provider failure pause after three failures within ten minutes
- Background cleanup of expired sessions and anonymous vote data
- Dedicated kiosk view with guest-link QR code
- Optional kiosk-owned audio with a browser user-gesture activation
- Screen Wake Lock request while the kiosk is visible
- Lifespan-managed cleanup for sessions inactive for 24 hours
- API tests covering queue ranking, lifecycle, QR generation, and playback failures
- Initial WebSocket endpoint for live-state integration
- Credential-gated YouTube search (`YOUTUBE_API_KEY`) with URL submission fallback
- Optional OIDC host login (`OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`)

Search requires `YOUTUBE_API_KEY`; without it, guests can still paste URLs. OIDC uses `/auth/callback` and requests the scopes configured by `OIDC_SCOPES`.

### Playback and automatic trimming

The player uses the official YouTube IFrame API. Regular browsers may continue playing when a tab is backgrounded, but mobile operating systems can suspend background tabs or stop playback when a device is locked. The kiosk can own playback when `PLAYBACK_OWNER=kiosk`; audio still depends on browser autoplay policy and the kiosk page must remain available to the operating system.

Screen Wake Lock is requested from the kiosk audio action and reacquired when the page becomes visible. Wake Lock requires a secure context (normally HTTPS) and is a best-effort browser feature; it cannot override operating-system power policies.

When `SPONSORBLOCK_ENABLED=1`, the server requests the categories configured in `SPONSORBLOCK_CATEGORIES` from [SponsorBlock](https://sponsor.ajay.app/). The browser seeks past matching segments during playback. `outro` normally covers end cards and credits; SponsorBlock does not have a separate `endcard` or `credits` category. `preview` can cover previews or recaps depending on the submitted segment, while `hook` is deliberately excluded by default because it may be part of the song itself. Coverage is incomplete and timestamps can be wrong, so videos without usable submissions play normally. SponsorBlock data does not alter or download the video, and it does not reliably skip YouTube advertisements.

Run tests with `uv run pytest -q`.
