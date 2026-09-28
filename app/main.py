import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.storage import connection, initialize


@asynccontextmanager
async def lifespan(application: FastAPI):
    initialize()
    yield


app = FastAPI(title="Karaoke", lifespan=lifespan)

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class NewClient(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class NewRequest(BaseModel):
    url: str = Field(max_length=500)


def youtube_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise HTTPException(422, "Link de vídeo do YouTube inválido") from None
    host = parsed.hostname
    if parsed.scheme != "https" or parsed.username or parsed.password or port or not host:
        raise HTTPException(422, "Use um link HTTPS de vídeo do YouTube")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"} and parsed.path == "/watch":
        identifiers = parse_qs(parsed.query).get("v", [])
        video_id = identifiers[0] if len(identifiers) == 1 else ""
    elif host == "youtu.be" and parsed.path.count("/") == 1:
        video_id = parsed.path[1:]
    else:
        video_id = ""
    if not VIDEO_ID.fullmatch(video_id):
        raise HTTPException(422, "Link de vídeo do YouTube inválido")
    return video_id


def authenticated_client(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sessão necessária")
    session_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with connection() as database:
        client = database.execute("SELECT id FROM clients WHERE session_hash = ?", (session_hash,)).fetchone()
    if client is None:
        raise HTTPException(401, "Sessão inválida")
    return client["id"]



@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/clients", status_code=201)
def create_client(payload: NewClient) -> dict[str, str]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Informe seu nome")
    client_id = str(uuid4())
    token = secrets.token_urlsafe(32)
    with connection() as database:
        database.execute(
            "INSERT INTO clients (id, name, session_hash) VALUES (?, ?, ?)",
            (client_id, name, hashlib.sha256(token.encode()).hexdigest()),
        )
    return {"client_id": client_id, "name": name, "token": token}


@app.post("/api/requests", status_code=201)
def create_request(payload: NewRequest, client_id: str = Depends(authenticated_client)) -> dict[str, str]:
    video_id = youtube_id(payload.url)
    request_id = str(uuid4())
    with connection() as database:
        database.execute(
            "INSERT INTO requests (id, client_id, video_id) VALUES (?, ?, ?)",
            (request_id, client_id, video_id),
        )
    return {"id": request_id, "status": "pending", "video_id": video_id}


@app.get("/api/requests")
def list_requests(client_id: str = Depends(authenticated_client)) -> list[dict]:
    with connection() as database:
        rows = database.execute(
            """SELECT requests.id, requests.video_id, requests.status, requests.title,
                      requests.error, requests.created_at, clients.name AS client_name
               FROM requests JOIN clients ON clients.id = requests.client_id
               ORDER BY requests.created_at, requests.rowid"""
        ).fetchall()
    return [dict(row) for row in rows]