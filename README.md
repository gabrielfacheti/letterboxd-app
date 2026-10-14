# letterboxd-analysis

A self-hosted website that turns a Letterboxd history into statistics and
taste-based recommendations. Live at **https://letterboxdstats.party**.

Give it a username (and, for the full history, the export ZIP you can download
from your own Letterboxd settings) and it shows:

- **Overview**: totals, rating distribution, activity per year, month and weekday
  (dated entries only, and it says how many), recent and top-rated films.
- **Deep dive**: decades, a world map of origin countries (zoomable, click a
  country to list its films), genres, languages, a "popular or niche?" score,
  runtime buckets, rewatches (the films you come back to, with a per-session
  timeline), recurring themes, and where your ratings differ from TMDB's.
- **People**: most-watched and best-rated directors and cast, hours spent with
  each, directors' gender and birthplaces.
- **Recommendations**: films you are likely to want to watch, with the reasons.

Any card can be saved or shared as an image.

## How it gets data, and how it respects Letterboxd

Only two sources, both sanctioned:

1. The **public RSS feed** of a username (`letterboxd.com/<user>/rss/`), which
   carries the ~50 most recent diary entries and the TMDB id of each film.
2. The **export ZIP** that the user downloads themselves and uploads here.

There is no scraping, no login, no stored credentials and no use of the
Letterboxd API. The [API](https://letterboxd.com/api-beta/) is in closed beta;
this project would like access to replace the RSS feed and the manual export
with it. Film metadata (genres, cast, crew, keywords, ratings) comes from
[TMDB](https://www.themoviedb.org/). **Ratings shown for films are TMDB's and are
never labelled as Letterboxd's**; the only Letterboxd ratings displayed are the
user's own, taken from their own diary.

Requests to Letterboxd are rate-limited per visitor (5 per minute), and the TMDB
cache expires after 180 days, as TMDB's terms require.

## Privacy

- The export is parsed **in memory and discarded**. `profile.csv` (e-mail, real
  name, location, bio) is **never opened**.
- A processed profile lives only at an **unguessable secret link**
  (`/p/<random token>`, new on every processing). Nothing lists profiles, and the
  link is never derived from the username.
- Pages make **no third-party requests** except poster and photo images from
  TMDB's CDN. No analytics, no web fonts, no framework from a CDN.
- Images exported from a card carry the username, never the secret link.

## Recommendation model

Each film is described by traits (directors, keywords, countries, genres, decade,
cast). A trait's weight is the log-odds of it appearing in the films you watched
versus a background corpus of popular films (`log P(trait | watched) − log
P(trait | background)`), and a candidate scores the mean weight of its traits.
Candidates come from TMDB's recommendations for your best-rated films. The model
reaches an AUC of **0.835** separating a user's watchlist from a neutral
background (0.877 on held-out watched films, against 0.724 for release year alone
and 0.666 for TMDB rating). A first version based on the user's ratings scored
0.53, no better than chance. Details and failed attempts are in
[`.claude/specs/recomendacao.md`](.claude/specs/recomendacao.md).

## Stack

Python 3.13, FastAPI, [Polars](https://pola.rs/), SQLite (TMDB cache, profiles,
jobs), Jinja2 templates, hand-written CSS and JavaScript. No JS framework, no
Celery or Redis: background work runs on `asyncio` in a single process.

## Running locally

```bash
uv sync
cp .env.example .env        # set TMDB_KEY (a free TMDB v3 API key)
uv run uvicorn letterboxd.web.app:app --reload   # http://127.0.0.1:8000
```

`LETTERBOXD_DATA_DIR` (default `./data`) holds the SQLite files. The first start
also builds a background corpus of films for the recommendations, which takes a
few minutes.

## Tests

```bash
uv run pytest -q
```

No test touches the network: HTTP is mocked with `httpx.MockTransport`, and the
RSS fixture in `tests/fixtures/` is a synthetic feed.

## Self-hosting

`compose.yaml` runs the app and a Cloudflare Tunnel connector in a hardened
container pair (read-only root filesystem, no capabilities, no published ports,
memory limit). Set `TMDB_KEY` and `TUNNEL_TOKEN` in `.env`, then
`docker compose up -d --build`. See [`.claude/specs/deploy.md`](.claude/specs/deploy.md)
for the details and the pitfalls.

## Credits

- This product uses the TMDB API but is not endorsed or certified by TMDB.
- Map outlines: [Natural Earth](https://www.naturalearthdata.com/) (public domain).
- [html-to-image](https://github.com/bubkoo/html-to-image) 1.11.11 (MIT), vendored
  in `src/letterboxd/web/static/vendor/`, powers the image export.
- Not affiliated with, endorsed by or connected to Letterboxd.

## License

[MIT](LICENSE) © 2026 Gabriel Facheti
