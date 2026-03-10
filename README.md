<div align="center">

<img src="static/moonshot-pff.png" alt="Moonshot Alpha" width="120"/>

#  Moonshot Alpha Bot

**Institutional-grade Solana trading terminal — built for the trenches.**

[![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)](https://python.org)
[![Solana](https://img.shields.io/badge/Solana-Mainnet-purple?logo=solana)](https://solana.com)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue?logo=telegram)](https://t.me/MoonshotAlphaBot)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

[Features](#features) • [Architecture](#architecture) • [Installation](#installation) • [Configuration](#configuration) • [Docs](https://moonshot-alpha-1.gitbook.io/docs.moonshotalpha.io/)

</div>

---

## Features

### 🔎 Forensic Scanner
Scans every token for red flags **before** you risk a single SOL:
- **Mint Authority** detection — flags if dev can print unlimited tokens
- **Freeze Authority** check — alerts if dev can freeze wallets
- **Honeypot analysis** — detects zero-sell-transaction patterns
- **Sybil wallet detection** — identifies artificial volume
- Risk score `0–100` with `LOW / MEDIUM / HIGH / CRITICAL` rating

### 🔫 Auto-Sniper
Two personality modes for every type of trader:
| Mode | Behaviour |
|------|-----------|
| **Safe** | Full forensic scan before firing. Max risk score configurable. |
| **Degen** | Fires fast on any new pair passing min-liquidity check. |

### 🔄 Swap Engine
Direct-to-validator execution:
- **Jupiter Aggregator** for best-route quotes across all Solana DEXes
- **Jito bundle submission** for MEV-protected, priority landing
- Configurable slippage (default 3%)

### 👻 Ghost Manager
Server-side position monitoring — protects capital while you sleep:
- Watches open positions every 10 seconds
- Auto-triggers **Take Profit** and **Stop Loss** sells
- Fires Telegram notification on every close

### ⭐ Wishlist & Alert System
- Add any Solana / EVM token by contract address
- Receive push alerts on major price moves
- Browse historical calls by risk category (High / Medium / Low)
- Mute/unmute alerts without leaving Telegram

---

## Architecture

```
moonshot-alpha-bot/
│
├── core/
│   ├── main.py              # Entry point — bot setup & polling
│   ├── bot_handlers.py      # All Telegram command & callback handlers
│   └── scheduler.py         # Background task runner (24h top-calls update)
│
├── database/
│   └── firebase.py          # Firestore: users, alerts, wishlists
│
├── scanner/
│   ├── token_scanner.py     # DEX feed scanner with filter pipeline
│   └── rug_interceptor.py   # On-chain forensic analysis engine
│
├── trading/
│   ├── sniper.py            # Auto-sniper (Safe & Degen modes)
│   ├── swap_engine.py       # Jupiter + Jito swap execution
│   └── position_monitor.py  # Ghost Manager — TP/SL monitoring
│
├── static/                  # Mini App frontend (HTML/CSS/JS)
│
├── .env.example             # Environment variable template
├── requirements.txt
└── README.md
```

---

## Installation

```bash
# 1. Clone
git clone https://github.com/KRUTHIKHARSHA/moonshot-alpha-bot.git
cd moonshot-alpha-bot

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# → fill in your keys (see Configuration below)

# 5. Run
python core/main.py
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ADMIN_CHAT_ID` | Your personal Telegram user ID |
| `FIREBASE_CREDENTIALS_BASE64` | Base64-encoded Firebase service account JSON |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `MINI_APP_URL` | URL of the deployed Mini App frontend |
| `PORT` | HTTP port for health-check server (default `8080`) |

> ⚠️ **Never commit your `.env` file.** It is already in `.gitignore`.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Bot framework | `python-telegram-bot` 21 |
| Blockchain | Solana (via RPC + Jupiter + Jito) |
| Database | Firebase Firestore + Supabase |
| DEX data | DexScreener API |
| HTTP client | `httpx` (async) |
| Web server | Flask (health-check endpoint) |
| ML model | scikit-learn / joblib (trend scoring) |

---

## Live Demo

🤖 **[@MoonshotAlphaBot](https://t.me/MoonshotAlphaBot)**  
📖 **[Full Docs](https://moonshot-alpha-1.gitbook.io/docs.moonshotalpha.io/)**  
💬 **[Community](https://t.me/MoonshotAlphaCommunity)**

---

## License

MIT © Kruthik Harsha B
