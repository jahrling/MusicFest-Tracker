import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
POSTERS_DIR = DATA_DIR / "posters"
GRAPH_DB_PATH = str(DATA_DIR / "graph.db")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-5"

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8080/callback")
SPOTIFY_SCOPE = "playlist-modify-public playlist-modify-private user-library-read"

SYNC_META_PATH = DATA_DIR / "sync_meta.json"

HOME_CITY = os.getenv("HOME_CITY", "Bettendorf, IA")
CONCERT_RADIUS_MILES = int(os.getenv("CONCERT_RADIUS_MILES", "100"))
RESEARCH_RANK_THRESHOLD = int(os.getenv("RESEARCH_RANK_THRESHOLD", "40"))

# Vision backend: "claude" (API, no GPU needed) or "paddleocr" (local, GPU recommended)
VISION_BACKEND = os.getenv("VISION_BACKEND", "claude").lower()

SCRAPE_DELAY_SECONDS = 1.5
BAND_MERGE_SIMILARITY_THRESHOLD = 0.90

# Ensure data dirs exist at import time
DATA_DIR.mkdir(exist_ok=True)
POSTERS_DIR.mkdir(exist_ok=True)
