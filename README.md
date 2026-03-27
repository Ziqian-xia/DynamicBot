# DynamicBot

A self-hosted platform for embedding multi-turn LLM interviews directly in Qualtrics surveys. Designed for social science researchers studying place-based topics (e.g., clean energy siting, infrastructure projects) where each respondent receives a conversation grounded in their local context.

Each deployment is independently owned — you bring your own OpenAI key, your own data, and your own server. No shared infrastructure, no data leaves your instance.

---

## How it works

1. You upload a dataset mapping geographic identifiers (e.g., Census Tract FIPS codes) to project details.
2. You write an interview design (context, question progression, max turns).
3. The admin panel generates ready-to-paste HTML + JS code for Qualtrics.
4. When a respondent reaches your Qualtrics question, the widget calls your server, the LLM receives only the facts from your dataset, and the full transcript is saved back to Qualtrics Embedded Data.

**Zero-hallucination guarantee:** The LLM is explicitly forbidden from presenting any facts not in your dataset. It can only describe what you uploaded.

---

## Deploy to Railway (recommended)

Railway is a one-click deployment platform. Your server runs in isolation — your OpenAI key and your data never leave your instance.

**Step 1 — Fork this repository** to your GitHub account.

**Step 2 — Create a new Railway project:**
1. Go to [railway.app](https://railway.app) and sign in with GitHub.
2. Click **New Project** → **Deploy from GitHub repo** → select your fork.
3. Railway will detect `nixpacks.toml` and start building automatically.

**Step 3 — Set environment variables** in Railway → your service → Variables:

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | Your OpenAI API key (`sk-...`). Get one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys). |
| `ADMIN_API_KEY` | Recommended | Password for the `/admin` panel. If not set, a random key is auto-generated and printed to startup logs. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATA_DIR` | Yes (prod) | Set to `/data` when using a Railway Volume (see Step 4). |

**Step 4 — Attach a persistent volume (required to keep your studies across deploys):**
1. In the Railway dashboard, go to your service → **Volumes** → **New Volume**.
2. Set the mount path to `/data`.
3. Add the environment variable `DATA_DIR=/data`.
4. Redeploy.

> Without a volume, your SQLite database (studies, configurations) is deleted every time Railway redeploys.

**Step 5 — Open the admin panel** at your Railway URL (e.g., `https://dynamicbot-production.up.railway.app`).

---

## Local development

```bash
# Clone and set up
git clone https://github.com/YOUR_USERNAME/DynamicBot.git
cd DynamicBot/backend

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Create your .env (copy from the example)
cp ../.env.example .env
# Edit .env: fill in OPENAI_API_KEY and ADMIN_API_KEY
# For local widget testing add: ALLOW_NULL_ORIGIN=true

# Start the server
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` to access the admin panel.

To test the Qualtrics widget locally, open `frontend/qualtrics_widget.html` directly in a browser (`file://`). It auto-connects to `localhost:8000` using a hardcoded test Census Tract.

---

## Using the admin panel

1. **Create a Study** (① Study Info tab): give your study a name.
2. **Upload your data** (② Project Data tab): paste JSON or upload a CSV. Each row is a geographic identifier (Census Tract, zip code, etc.) mapped to project details. A `DEFAULT` row is required as a fallback.
3. **Design the interview** (③ Interview Design tab): write the research context, question progression per turn, max turns, and temperature.
4. **Get the embed code** (④ Embed in Qualtrics tab): copy the HTML and JS blocks. The tab also includes a complete step-by-step Qualtrics integration guide.

### Data format

**JSON** (paste directly):
```json
{
  "37063020100": {
    "project_name": "Durham Solar Array",
    "status": "Under Construction",
    "jobs_created": 340,
    "timeline": "Groundbreaking Q1 2024, operational Q3 2025",
    "location": "Durham County, NC",
    "technology_type": "Utility-scale solar PV + battery storage",
    "developer": "SunPath Energy LLC",
    "investment_amount": "$420 million",
    "ira_tax_credit": "48C Advanced Energy Manufacturing Tax Credit",
    "community_benefit": "Priority hiring for Durham County residents"
  },
  "DEFAULT": {
    "project_name": "Regional Clean Energy Initiative",
    ...
  }
}
```

**CSV** (upload via the admin panel — use the "CSV Template" button to download the correct column headers).

---

## Qualtrics integration (overview)

The admin panel's **Embed in Qualtrics** tab contains the full step-by-step guide. In brief:

1. Add `CensusTract` and `ChatTranscript` fields to your Survey Flow Embedded Data.
2. Create a **Text/Graphic** question where you want the chat to appear.
3. Paste the **HTML code** into the question's HTML editor.
4. Paste the **JS code** into the question's JavaScript tab.
5. Set Force Response to **OFF** on that question.
6. Test in Preview mode.

After data collection, the `ChatTranscript` column in your exported CSV contains the full conversation in the format:
```
[BOT]: message

[USER]: response

[BOT]: follow-up
```

---

## Security notes

- **Your OpenAI API key is never sent to the browser.** It is used only server-side in Railway environment variables.
- **Admin routes** (`/api/admin/*`) require the `ADMIN_API_KEY` Bearer token. Public routes (`/start`, `/chat`) are intentionally accessible but rate-limited.
- **Rate limiting:** 300 `/start` requests and 1,500 `/chat` requests per IP per hour. This handles up to 300 simultaneous respondents behind a shared NAT (e.g., a university network) while blocking single-IP abuse.
- **Input validation:** user messages are capped at 2,000 characters server-side.
- **No PII stored:** the server stores only study configurations and in-memory session state. Transcripts are written directly to Qualtrics and never persisted on the server.

---

## Environment variables reference

See [`.env.example`](.env.example) for a complete annotated reference.

---

## Project structure

```
DynamicBot/
├── backend/
│   ├── app.py            # FastAPI server — chat API + admin API
│   ├── storage.py        # SQLite-backed study persistence
│   ├── requirements.txt
│   └── static/
│       └── admin.html    # Admin panel (single-file SPA)
├── frontend/
│   ├── qualtrics_widget.html   # Chat UI — paste into Qualtrics HTML editor
│   └── qualtrics_widget.js     # Chat logic — paste into Qualtrics JS tab
├── .env.example          # Environment variable reference
├── nixpacks.toml         # Railway build configuration
└── railway.toml          # Railway deploy configuration
```
