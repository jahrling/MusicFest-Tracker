# Festival Band Tracker

Ingest festival posters with Claude's vision API, research bands, track their lineup rank over time, and generate Spotify playlists from curated lists.

---

## Setup

### 1. Python environment

```bash
cd festival-tracker
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `SPOTIFY_CLIENT_ID` | [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) |
| `SPOTIFY_CLIENT_SECRET` | Same dashboard |
| `SPOTIFY_REDIRECT_URI` | Set to `http://127.0.0.1:8080/callback` in the dashboard |
| `HOME_CITY` | Your city for local concert radius search |
| `CONCERT_RADIUS_MILES` | Radius for "upcoming local shows" research |
| `RESEARCH_RANK_THRESHOLD` | Bands ranked below this % get researched first (default 40) |
| `VISION_BACKEND` | `claude` (default) or `paddleocr` — see section below |

---

## Vision backends

### Option A — Claude API (default, no GPU needed)

Leave `VISION_BACKEND=claude` in `.env`. Requires `ANTHROPIC_API_KEY`. Claude vision reads the poster image directly and returns structured JSON in one call.

### Option B — PaddleOCR (local, GPU recommended)

Set `VISION_BACKEND=paddleocr`. No Anthropic key required for ingestion.

**Install PaddleOCR — pick one:**

```bash
# CPU only (works on any machine, slower)
pip install paddleocr paddlepaddle

# NVIDIA GPU (CUDA) — much faster, recommended once you have a GPU
pip install paddleocr paddlepaddle-gpu
```

> Do **not** install both `paddlepaddle` and `paddlepaddle-gpu` at the same time.

**How it works:**

1. **PaddleOCR** extracts every text block with 4-corner bounding boxes. Fully local — no network call.
2. **Rank (1–100)** is derived from bounding-box height, a reliable proxy for font size.
3. **Noise filter** strips dates, URLs, ticket/venue boilerplate via regex.
4. **Music platform cross-reference** — for each remaining candidate, the validator queries:
   - **iTunes / Apple Music Search API** — free, no credentials, always tried first
   - **Spotify artist search** — tried if `SPOTIFY_CLIENT_ID`/`SECRET` are set (Client Credentials OAuth, no user login required). Also captures the **Spotify artist ID** for free at ingest time.
   - **MusicBrainz** — free fallback, rate-limited to 1 req/sec; good for niche artists
   
   If the platform returns a name with ≥ 80% similarity to the OCR text, it's confirmed as a real artist and the platform's canonical spelling is used (fixing OCR errors). Intentionally misspelled names like *Phish*, *Ludacris*, *!!!* match exactly and are left alone.
   
   Candidates that match nothing on any platform are still included, just marked `unverified`.

5. **Alphabetical detection**: if ≥ 75% of confirmed names are A–Z when read top-to-bottom, ranks are set to `null`.

> **No `ANTHROPIC_API_KEY` is used when `VISION_BACKEND=paddleocr`** — not even for a cheap text call. The validator is purely music platform data.

**Dice.fm**: they don't expose a public search API. Their show listings are picked up by the research agent's `web_search` tool during the band research phase.

**Limitations vs. Claude vision:**

| | Claude vision | PaddleOCR |
|---|---|---|
| Stylized / overlapping fonts | Excellent | Good |
| Band name vs. sponsor text | Excellent | Good (validator confirms) |
| PDFs | Supported | Not supported — convert to PNG first |
| GPU required | No | No (CPU works, GPU is faster) |
| API cost per poster | Yes (vision tokens) | No |
| Spotify ID at ingest | No | Yes (captured by validator) |

### 3. Spotify app setup

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Create an app
3. Add `http://localhost:8080/callback` as a Redirect URI
4. Copy Client ID and Secret into `.env`

The first time you use the Playlist builder, a browser window opens for OAuth. The token is cached in `.spotify_cache`.

---

## Running the app

```bash
# From the festival-tracker/ directory
uvicorn api.main:app --reload --port 8080
```

Open [http://localhost:8080](http://localhost:8080).

---

## Usage

### Ingesting a poster

**From URL** — paste a direct link to a festival poster image (JPG, PNG, WEBP) or PDF.

**Upload** — drag a local file onto the upload input.

Claude's vision model extracts:
- Festival name, date(s), location
- Full band lineup
- Lineup rank (1–100%) for non-alphabetical posters — 100 = headliner

### Researching bands

On any band detail page, click **Research Band** to run the research agent. It uses Claude with web search to populate:
- Genres and positioning blurb
- Musical influences
- YouTube links
- Upcoming local shows (within your configured radius)
- Upcoming notable festival appearances
- Bubble status: `hot` / `bubbling` / `stagnant` / `declining`

To batch-research all bands from a festival, POST to `/research/batch` with the `festival_id`. Bands ranked below `RESEARCH_RANK_THRESHOLD` are prioritized.

### Rating bands

On the band detail page, assign a score (1–10), interest level (`skip` / `curious` / `interested` / `must-see`), and optional notes. Every save is timestamped in the graph.

### Timeline view

`/timeline/view` shows all band-festival edges sorted by date with rank bars. Export to CSV via the **Export CSV** button or `GET /timeline.csv`.

### Spotify playlist

`/playlist/view` — filter by:
- Minimum rating
- Interest levels (e.g. `interested, must-see`)
- Bubble statuses (e.g. `hot, bubbling`)
- Specific festival

The builder is idempotent — re-running the same filter set updates the existing playlist rather than creating a duplicate.

---

## Project structure

```
festival-tracker/
├── ingestion/
│   ├── poster_parser.py    # Claude vision extraction + rank normalization
│   ├── image_utils.py      # URL download, base64 encoding, resize
│   └── ingest.py           # Orchestrates parse → graph upsert
├── graph/
│   ├── schema.py           # Kuzu DDL (nodes + relationships)
│   ├── db.py               # Single lazy connection
│   └── queries.py          # All graph read/write helpers
├── research/
│   ├── band_agent.py       # Claude web-search research agent
│   ├── concert_finder.py   # Persists upcoming shows to graph
│   ├── bubble_scorer.py    # Rank trend → hot/bubbling/stagnant/declining
│   └── research_pipeline.py # Batch orchestration with priority ordering
├── ratings/
│   └── rating_service.py   # Timestamped rating CRUD
├── spotify/
│   └── playlist_builder.py # OAuth, idempotent playlist creation
├── api/
│   └── main.py             # FastAPI app (all routes)
├── ui/
│   ├── templates/          # Jinja2 + HTMX templates
│   └── static/             # CSS + JS
├── data/
│   ├── posters/            # Downloaded/uploaded poster images
│   └── graph.db            # Kuzu embedded graph database
├── config.py               # All settings, reads from .env
└── requirements.txt
```

---

## Graph schema

**Nodes:** `Band`, `Festival`, `Concert`, `Person`

**Relationships:**
- `(Band)-[:PLAYED_AT {rank, alphabetical, timestamp}]->(Festival)`
- `(Band)-[:SCHEDULED_FOR {rank, alphabetical, timestamp}]->(Festival)`
- `(Band)-[:PERFORMED_AT {date}]->(Concert)`
- `(Band)-[:INFLUENCED_BY {label}]->(Band)`
- `(Person)-[:RATED {score, interest, notes, timestamp}]->(Band)`

Ranks are stored as `DOUBLE`. Alphabetical posters get `rank = -1.0` and `alphabetical = true` on their edges.

---

## Band merging

When a band appears on a new poster, the name is fuzzy-matched against all existing Band nodes using `difflib.SequenceMatcher`. A match score ≥ 0.90 merges rather than creates a new node. The match threshold is configurable via `BAND_MERGE_SIMILARITY_THRESHOLD` in `config.py`.

---

## Notes & limitations

- The research agent requires Claude's `web_search` tool, available on `claude-sonnet-4-20250514`.
- Kuzu is embedded — no separate database server needed. The graph file lives at `data/graph.db`.
- Spotify top tracks default to `country="US"` — change in `playlist_builder.py` if needed.
- Scraping respects a `SCRAPE_DELAY_SECONDS` delay between research calls (default 1.5s).
