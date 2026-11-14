"""Settings: reads .env, holds the market/role keyword lists used by the cheap pre-filter."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _load_env() -> None:
    env = ROOT / ".env"
    try:
        lines = env.read_text().splitlines()
    except OSError:  # missing, or root-only on the server (systemd already loaded it)
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


_load_env()

AI = os.environ.get("HUNT_AI", "chatgpt").lower()  # "chatgpt" (ChatGPT plan) or "claude" (API key)
CHATGPT_MODEL = os.environ.get("HUNT_CHATGPT_MODEL", "")  # empty = first model your account lists
CHATGPT_EFFORT = os.environ.get("HUNT_CHATGPT_EFFORT", "")  # optional reasoning effort, e.g. "medium"
MODEL = os.environ.get("HUNT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("HUNT_EFFORT", "medium")
MAX_AI_PER_DAY = int(os.environ.get("HUNT_MAX_AI_PER_DAY", "60"))
MIN_SCORE = int(os.environ.get("HUNT_MIN_SCORE", "60"))
MAX_AI_PER_RUN = int(os.environ.get("HUNT_MAX_AI_PER_RUN", "25"))
DB_PATH = Path(os.environ.get("HUNT_DB", str(DATA / "hunt.db")))
RUN_EVERY_MINUTES = int(os.environ.get("HUNT_RUN_EVERY_MINUTES", "60"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# A lead must mention at least one market word (and, for job posts, one role word)
# before it is worth paying the AI to look at it.
MARKET_WORDS = {
    "crypto": ["crypto", "bitcoin", "btc", "ethereum", "blockchain", "web3", "defi", "token",
               "nft", "solana", "stablecoin", "dao", "memecoin", "onchain", "on-chain", "presale",
               "exchange", "wallet", "tokenomics"],
    "forex": ["forex", "fx", "metatrader", "mt4", "mt5", "cfd", "prop firm", "prop trading",
              "proprietary trading", "funded trader", "broker", "currency trading"],
    "us_stocks": ["stock", "stocks", "equities", "equity research", "investing", "investor",
                  "brokerage", "trading", "trader", "hedge fund", "asset management", "wealth",
                  "fintech", "robo-advisor", "market data", "nasdaq", "nyse", "capital markets"],
    "india_stocks": ["nse", "bse", "sebi", "nifty", "sensex", "demat", "zerodha", "upstox",
                     "groww", "mutual fund", "stockbroking", "wealthtech", "dalal street"],
    "options": ["options trading", "option chain", "derivatives", "f&o", "futures", "options"],
}

# Job posts must be a role the owner sells (writing, analysis, design, video, community, web/bot builds)…
ROLE_WORDS = ["writer", "writing", "content", "copywriter", "copy", "editor", "research analyst",
              "market analyst", "analyst", "researcher", "research", "strategist", "designer", "design",
              "ui", "ux", "figma", "video", "motion", "youtube", "creator", "social media", "community",
              "seo", "ghostwriter", "whitepaper", "educator", "instructor", "tutor", "script", "marketing",
              "frontend", "front-end", "web developer", "full-stack", "fullstack", "trading bot", "bot",
              "automation", "tradingview", "pine script", "mql", "freelance", "contract", "part-time"]
# …and not a role he doesn't do.
JOB_EXCLUDE = ["rust", "smart contract", "solidity", "qa", "kyc", "aml", "risk", "compliance", "sales",
               "account executive", "business development", "bd ", "hr", "recruiter", "talent", "devops",
               "security", "sre", "infrastructure", "backend", "back-end", "data engineer", "accountant",
               "legal", "counsel", "intern", "audit", "payroll", "treasury", "operations"]
