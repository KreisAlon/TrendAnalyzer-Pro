# TrendAnalyzer Pro

> A Multi-Platform System for Collecting, Filtering and Analyzing Technology Trends in Developer Communities.

Developed as a Final Project for the B.Sc. in Computer Science at Tel-Hai College.  
**Author:** Alon Kreisberger  
**Advisor:** Dr. Enbal Ronen  

---

## About the Project
**TrendAnalyzer Pro** is an asynchronous, three-tier architecture system designed to aggregate, filter, and analyze technology trends across fragmented developer ecosystems. The system pulls real-time data concurrently from four distinct platforms:
1. **GitHub** (Implementation & open-source code)
2. **Hacker News** (Industry debate & linked articles)
3. **Dev.to** (Practitioner write-ups & guides)
4. **Mastodon** (Short-form public discussions)

## Core Architecture & Features
- **The Gatekeeper (Quality Filter):** A rigorous, 5-stage deterministic quality filter (Structure, Relevance, Uniqueness, Language, and Themes via KeyBERT) that discards noise (spam, bots, paywalls) before expensive NLP models process the data.
- **Normalized Trend Scoring:** Combines log scaling, Z-scores, and Sigmoid functions to normalize incomparable engagement metrics (stars, upvotes, reactions) into a unified 0–100 scale per platform.
- **Semantic Similarity Graph & Emerging Bridges:** Uses `all-MiniLM-L6-v2` sentence embeddings and NetworkX modularity optimization to detect "structural holes" and emerging cross-community bridges between distant developer circles.
- **Interactive Presentation Tier:** Built with **Streamlit**, featuring an Ecosystem Sunburst Wheel, Hype vs. Reality verdicts, Evidence Cards for emerging connections, and a Gatekeeper funnel inspector.
- **Autonomous Background Worker:** Powered by APScheduler to discover and track trending developer topics automatically every two hours.

---

## Tech Stack
- **Backend & Core Engine:** Python, `asyncio`, `httpx`, `APScheduler`
- **NLP & Data Science:** `KeyBERT`, `VADER Sentiment`, `Sentence-Transformers` (MiniLM), `NetworkX`
- **Database:** SQLite (configured with Write-Ahead Logging - WAL)
- **UI / Dashboard:** Streamlit, Plotly

---

## Getting Started

### Prerequisites
- Python 3.9+
- Pipenv or virtualenv (recommended)

### Installation
1. Clone the repository:
   ```bash
   git clone [https://github.com/KreisAlon/TrendAnalyzer-Pro.git](https://github.com/KreisAlon/TrendAnalyzer-Pro.git)
   cd TrendAnalyzer-Pro

### Install dependencies:
    pip install -r requirements.txt

### Run the application:
    python main.py