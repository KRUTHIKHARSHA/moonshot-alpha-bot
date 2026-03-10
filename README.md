# ⚡ Autonomous Trading Engine (Moonshot Alpha)

[![Documentation](https://img.shields.io/badge/docs-GitBook-blue.svg)](https://moonshot-alpha-1.gitbook.io/docs.moonshotalpha.io)
[![Python](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![Supabase](https://img.shields.io/badge/Database-Supabase-green.svg)](https://supabase.com/)
[![Firebase](https://img.shields.io/badge/Auth-Firebase-orange.svg)](https://firebase.google.com/)

[📚 Read the Official Documentation & User Guide](https://moonshot-alpha-1.gitbook.io/docs.moonshotalpha.io)

## 📌 Overview
This repository contains the system architecture for an institutional-grade, multi-chain (Solana & EVM) crypto trading terminal. Designed to operate entirely within a Telegram Mini App, the system features real-time blockchain event ingestion, AI-driven risk analysis, tiered asynchronous market tracking, and custom MEV-protected execution routing.

> ⚠️ **Note for Hiring Managers & Reviewers:** > This public repository serves as an architectural and engineering showcase. Because this engine is currently running in production, proprietary execution algorithms (such as the exact `auto_sniper.py` trigger logic, anti-rug heuristics, and trained machine learning models) have been redacted or replaced with simulated interfaces. The code provided demonstrates system design, asynchronous data pipelines, database integration, and blockchain interaction.

---

## 🏗️ System Architecture

The engine is built on a microservice-inspired architecture using heavily optimized `asyncio` loops to handle high-frequency blockchain data without blocking the main event loop.

### 1. The Intelligence Layer (`hunter_scanner.py` & `ai_engine.py`)
* **Multi-Chain Event Ingestion:** Listens to Helius RPC (Solana) and Moralis (EVM) for real-time liquidity pool creations and token launches.
* **Purity Engine:** Automatically decodes base64 on-chain transaction data to analyze holder distribution, detect Sybil clusters, and flag potential "rug pulls" or honeypots before exposing tokens to users.
* **AI Flow Analysis:** Tracks God-whale vs. Minnow inflows in real-time to compute buy-pressure and momentum scoring.

### 2. The Tracking Engine (`peakgain_tracker.py`)
* **Tiered Polling Architecture:** Manages DexScreener API rate limits by tiering database updates.
  * *Tier 1 (Hotlist):* Updates Top 30 gainers and tokens <2 hours old every 2 minutes.
  * *Tier 2 (Contenders):* Updates mid-tier tokens every 15 minutes.
  * *Tier 3 (Deep Radar):* Background tracking every 2 hours.
  * *Tier 4 (Grim Reaper):* Purges dead/drained tokens older than 48 hours to optimize database storage and compute.
* **Real-Time Algorithms:** Computes a live `trending_score` by comparing moving-window volume (m5, h1) against live liquidity and transaction velocities.

### 3. The Execution Layer (`swap_engine.py`)
* **Jupiter Aggregation:** Routes swaps through Jupiter to find optimal execution paths across Raydium, Meteora, and Orca.
* **MEV Protection (Jito):** Wraps atomic swap transactions in Jito Bundles with dynamic validator tips to bypass the public mempool, completely shielding users from sandwich attacks and front-running.
* **Smart RPC Fallback:** If Jito validation fails, the system automatically falls back to a custom high-speed RPC pool to guarantee execution.

### 4. The Frontend & State (`main.py` & `index.html`)
* **Hybrid Database State:** Uses **Firebase** for user authentication, Telegram Stars payment webhooks, and subscription tracking. Uses **Supabase (PostgreSQL)** for high-speed, high-volume time-series data storage (token metrics).
* **Native Telegram Mini App:** Bypasses clunky bot commands by rendering a native, responsive HTML/JS terminal directly over the chat interface, communicating state securely back to the Python backend.

---

## 🛠️ Tech Stack

* **Backend:** Python 3.10+, `asyncio`, `httpx`, `Flask` (for webhooks)
* **Databases:** Supabase (PostgreSQL), Firebase Firestore
* **Blockchain Interfaces:** `solders`, `solana-py`, Helius API, Moralis API
* **DeFi Integrations:** Jupiter V6 API, Jito Block Engine, DexScreener API
* **Frontend:** HTML5, CSS3 (Glassmorphism UI), Telegram Web App JS SDK

---

## 🚀 Setup & Installation (Simulated)

```bash
# Clone the repository
git clone [https://github.com/YOUR_GITHUB_USERNAME/autonomous-trading-engine.git](https://github.com/YOUR_GITHUB_USERNAME/autonomous-trading-engine.git)

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Add your Supabase, Firebase, and Telegram credentials

# Run the core services
python main.py
python peakgain_tracker.py
