import asyncio
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from openai import AsyncOpenAI, RateLimitError, APIStatusError
from pydantic import BaseModel, Field

import storage

load_dotenv()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="DynamicBot API", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# CORS
# Qualtrics hosts surveys on subdomains like yale.co1.qualtrics.com.
# `allow_origins` does exact-string matching in Starlette, so a wildcard
# string like "https://*.qualtrics.com" does NOT work — use allow_origin_regex.
# The "null" origin (for file:// local widget testing) is opt-in via env var.
# ---------------------------------------------------------------------------

_origins = [
    "https://survey.qualtrics.com",
    "http://localhost:3000",
    "http://localhost:8000",
]
if os.environ.get("ALLOW_NULL_ORIGIN", "false").lower() == "true":
    _origins.append("null")   # enables file:// widget testing in dev

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=r"https://[a-zA-Z0-9\-]+\.qualtrics\.com",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ---------------------------------------------------------------------------
# In-memory session store
# Shape: {session_id: {messages, turn_count, max_turns, model, temperature, created_at}}
# ---------------------------------------------------------------------------

sessions: dict[str, dict] = {}
SESSION_TTL = 7200  # 2 hours

# ---------------------------------------------------------------------------
# In-memory rate limiter (no external dependencies)
# Protects /start and /chat from credit-abuse by bad actors who discover the URL.
# ---------------------------------------------------------------------------

_rate_store: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()

RATE_WINDOW = 3600   # 1 hour rolling window
RATE_START  = 300    # max /start calls per IP per hour
                     # 30 was too low for university NAT (100 respondents → same IP)
RATE_CHAT   = 1500   # max /chat calls per IP per hour
                     # 300 respondents × 5 turns each = 1500 max from same NAT


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, accounting for Railway / proxy X-Forwarded-For."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client and request.client.host) or "unknown"


def _check_rate(ip: str, bucket: str, limit: int) -> bool:
    """Return True (allowed) or False (rate-limited)."""
    key = f"{bucket}:{ip}"
    now = time.time()
    cutoff = now - RATE_WINDOW
    with _rate_lock:
        calls = [t for t in _rate_store[key] if t > cutoff]
        if len(calls) >= limit:
            return False
        calls.append(now)
        _rate_store[key] = calls
        return True


# ---------------------------------------------------------------------------
# Lazy OpenAI client — reads API key from DB at call time
# ---------------------------------------------------------------------------

def _get_openai_client() -> AsyncOpenAI:
    """Return an AsyncOpenAI client using the key stored in the DB."""
    key = storage.get_config("openai_api_key")
    if not key:
        raise HTTPException(503, "OpenAI API key not configured. Visit the admin panel to set it up.")
    return AsyncOpenAI(api_key=key)


# ---------------------------------------------------------------------------
# Startup / background tasks
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    storage.init_db()
    asyncio.create_task(_cleanup_sessions())
    print("DynamicBot API ready.")


async def _cleanup_sessions() -> None:
    while True:
        await asyncio.sleep(3600)
        cutoff = time.time() - SESSION_TTL
        expired = [sid for sid, s in list(sessions.items()) if s.get("created_at", 0) < cutoff]
        for sid in expired:
            sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_ct(ct: str) -> str:
    return re.sub(r"[\s\-]", "", ct.strip())


def build_system_prompt(project: dict, study: dict) -> str:
    field_labels = {
        "project_name":      "Project Name",
        "status":            "Status",
        "location":          "Location",
        "technology_type":   "Technology Type",
        "developer":         "Developer",
        "investment_amount": "Investment Amount",
        "jobs_created":      "Jobs Created",
        "timeline":          "Timeline",
        "ira_tax_credit":    "IRA Tax Credit Used",
        "community_benefit": "Community Benefit",
    }
    project_lines = "\n".join(
        f"{field_labels.get(k, k.replace('_', ' ').title())}: {v}"
        for k, v in project.items()
    )
    max_turns = study["max_turns"]
    ordinals = {1: "st", 2: "nd", 3: "rd"}
    suffix = ordinals.get(max_turns, "th")

    return f"""{study['research_context']}

## PROJECT DATA — YOUR ONLY SOURCE OF FACTS

The following is the verified, complete record for this project. Present ONLY these facts. Do not add, infer, estimate, or speculate beyond what appears below.

{project_lines}

## NON-NEGOTIABLE DATA-GROUNDING RULES
1. Every factual claim must appear verbatim in the PROJECT DATA above.
2. If asked about something not listed: "I don't have that specific detail, but I'd love to hear your thoughts on what we do know."
3. Never invent statistics, names, dates, or descriptions not in the data.
4. Never express personal opinions or policy positions.
5. Keep each response under 200 words.
6. The conversation lasts exactly {max_turns} respondent turn{'s' if max_turns != 1 else ''}. After the respondent's {max_turns}{suffix} message, close gracefully.

## INTERVIEW STRUCTURE
{study['question_guidance']}"""


async def call_openai(messages: list, model: str, temperature: float) -> str:
    client = _get_openai_client()
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=400,
        presence_penalty=0.1,
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Static / root routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin")


@app.get("/admin", include_in_schema=False)
async def admin_ui():
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Chat API — public endpoints
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    census_tract: str
    study_id: str


class StartResponse(BaseModel):
    session_id: str
    message: str
    turn_count: int


class ChatRequest(BaseModel):
    session_id: str
    user_message: str = Field(..., max_length=2000)   # prevent token-cost abuse


class ChatResponse(BaseModel):
    message: str
    turn_count: int
    conversation_complete: bool


@app.post("/start", response_model=StartResponse)
async def start(req: StartRequest, request: Request):
    # Rate limiting
    ip = _get_client_ip(request)
    if not _check_rate(ip, "start", RATE_START):
        raise HTTPException(429, "Too many requests — please try again later.")

    study = storage.get_study(req.study_id)
    if study is None:
        raise HTTPException(404, "Study not found")

    ct = normalize_ct(req.census_tract)
    project = study["projects_data"].get(ct) or study["projects_data"].get("DEFAULT")
    if project is None:
        raise HTTPException(404, "Census tract not found and no DEFAULT entry in study data")

    messages = [{"role": "system", "content": build_system_prompt(project, study)}]

    try:
        reply = await call_openai(messages, study["model"], study["temperature"])
    except RateLimitError:
        raise HTTPException(503, "The AI service is temporarily busy. Please try again in a moment.")
    except APIStatusError as exc:
        raise HTTPException(502, "AI service returned an error. Please try again.")
    except Exception:
        raise HTTPException(500, "An unexpected error occurred. Please try again.")

    messages.append({"role": "assistant", "content": reply})
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "messages":    messages,
        "turn_count":  0,
        "max_turns":   study["max_turns"],
        "model":       study["model"],        # stored so /chat uses correct model
        "temperature": study["temperature"],  # stored so /chat uses correct temp
        "created_at":  time.time(),
    }
    return StartResponse(session_id=session_id, message=reply, turn_count=0)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    # Rate limiting
    ip = _get_client_ip(request)
    if not _check_rate(ip, "chat", RATE_CHAT):
        raise HTTPException(429, "Too many requests — please try again later.")

    session = sessions.get(req.session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if session["turn_count"] >= session["max_turns"]:
        raise HTTPException(400, "Conversation already complete")

    session["messages"].append({"role": "user", "content": req.user_message})

    try:
        reply = await call_openai(
            session["messages"],
            session["model"],        # use per-study model
            session["temperature"],  # use per-study temperature
        )
    except RateLimitError:
        session["messages"].pop()
        raise HTTPException(503, "The AI service is temporarily busy. Please try again in a moment.")
    except APIStatusError:
        session["messages"].pop()
        raise HTTPException(502, "AI service returned an error. Please try again.")
    except Exception:
        session["messages"].pop()
        raise HTTPException(500, "An unexpected error occurred. Please try again.")

    session["messages"].append({"role": "assistant", "content": reply})
    session["turn_count"] += 1
    complete = session["turn_count"] >= session["max_turns"]

    return ChatResponse(message=reply, turn_count=session["turn_count"], conversation_complete=complete)


# ---------------------------------------------------------------------------
# Admin API — configuration
# ---------------------------------------------------------------------------

class OpenAIKeyPayload(BaseModel):
    api_key: str = Field(..., min_length=10)


@app.get("/api/admin/config/status")
async def admin_config_status():
    """Returns whether the OpenAI API key has been configured."""
    key = storage.get_config("openai_api_key")
    return {"configured": bool(key)}


@app.post("/api/admin/config/openai-key", status_code=200)
async def admin_set_openai_key(payload: OpenAIKeyPayload):
    """Save the OpenAI API key to the database."""
    storage.set_config("openai_api_key", payload.api_key.strip())
    return {"saved": True}


# ---------------------------------------------------------------------------
# Admin API — studies
# ---------------------------------------------------------------------------

class StudyPayload(BaseModel):
    name: str
    description: str = ""
    projects_data: dict
    research_context: str
    question_guidance: str
    max_turns: int = 3
    temperature: float = 0.3
    model: str = "gpt-4o"


@app.get("/api/admin/studies")
async def admin_list_studies():
    return storage.list_studies()


@app.post("/api/admin/studies", status_code=201)
async def admin_create_study(payload: StudyPayload):
    return storage.create_study(payload.model_dump())


@app.get("/api/admin/studies/{study_id}")
async def admin_get_study(study_id: str):
    study = storage.get_study(study_id)
    if not study:
        raise HTTPException(404, "Study not found")
    return study


@app.put("/api/admin/studies/{study_id}")
async def admin_update_study(study_id: str, payload: StudyPayload):
    study = storage.update_study(study_id, payload.model_dump())
    if not study:
        raise HTTPException(404, "Study not found")
    return study


@app.delete("/api/admin/studies/{study_id}")
async def admin_delete_study(study_id: str):
    if not storage.delete_study(study_id):
        raise HTTPException(404, "Study not found")
    return {"deleted": True}


@app.get("/api/admin/studies/{study_id}/embed")
async def admin_embed_code(study_id: str, request: Request):
    study = storage.get_study(study_id)
    if not study:
        raise HTTPException(404, "Study not found")

    backend_url = str(request.base_url).rstrip("/")
    # Railway terminates TLS at the proxy; base_url arrives as http:// — force https://
    if backend_url.startswith("http://") and "localhost" not in backend_url:
        backend_url = "https://" + backend_url[len("http://"):]
    root = Path(__file__).parent.parent

    widget_js   = (root / "frontend" / "qualtrics_widget.js").read_text()
    widget_html = (root / "frontend" / "qualtrics_widget.html").read_text()

    # Substitute the three configurable constants in the widget JS
    js = re.sub(r"const BACKEND_URL\s*=\s*'[^']*';[^\n]*",
                f"const BACKEND_URL = '{backend_url}';", widget_js)
    js = re.sub(r"const STUDY_ID\s*=\s*'[^']*';[^\n]*",
                f"const STUDY_ID = '{study_id}';", js)
    js = re.sub(r"const MAX_TURNS\s*=\s*\d+;",
                f"const MAX_TURNS = {study['max_turns']};", js)

    header = (
        f"// ============================================================\n"
        f"// DynamicBot — Auto-generated Qualtrics Widget JS\n"
        f"// Study: {study['name']}\n"
        f"// Study ID: {study_id}\n"
        f"// ============================================================\n"
    )
    return {
        "study_name":  study["name"],
        "html":        widget_html,
        "js":          header + js,
        "backend_url": backend_url,
        "study_id":    study_id,
    }


@app.get("/api/admin/defaults")
async def admin_defaults():
    return {
        "research_context": storage.DEFAULT_RESEARCH_CONTEXT,
        "question_guidance": storage.DEFAULT_QUESTION_GUIDANCE,
    }
