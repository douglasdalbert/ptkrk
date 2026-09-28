import asyncio
import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.invitations import accept_invitation, expire_invitation, invitation_state
from app.media import MEDIA_ROOT
from app.storage import connection, initialize


@asynccontextmanager
async def lifespan(application: FastAPI):
    initialize()
    watcher = asyncio.create_task(watch_requests())
    try:
        yield
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Karaoke", lifespan=lifespan)
SINGER_ROOT = Path(__file__).resolve().parent.parent / "app-cantor"
app.mount("/cantor/assets", StaticFiles(directory=SINGER_ROOT), name="cantor-assets")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
subscribers: set[WebSocket] = set()


class NewClient(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class NewRequest(BaseModel):
    youtubeCode: str = Field(min_length=11, max_length=11)


def youtube_id(code: str) -> str:
    if not VIDEO_ID.fullmatch(code):
        raise HTTPException(422, "Código de vídeo do YouTube inválido")
    return code


def client_for_token(token: str) -> str | None:
    session_hash = hashlib.sha256(token.encode()).hexdigest()
    with connection() as database:
        client = database.execute("SELECT id FROM clients WHERE session_hash = ?", (session_hash,)).fetchone()
    return client["id"] if client else None


def authenticated_client(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sessão necessária")
    client_id = client_for_token(authorization[7:])
    if client_id is None:
        raise HTTPException(401, "Sessão inválida")
    return client_id


def request_snapshot() -> list[dict]:
    with connection() as database:
        rows = database.execute(
            """SELECT requests.id, requests.client_id, requests.video_id, requests.status, requests.title,
                 requests.error, requests.created_at, clients.name AS client_name,
                 ready_queue.position AS position
               FROM requests JOIN clients ON clients.id = requests.client_id
             LEFT JOIN ready_queue ON ready_queue.request_id = requests.id
             ORDER BY CASE WHEN requests.status = 'ready' AND ready_queue.position IS NOT NULL
                     THEN 0 ELSE 1 END,
                   ready_queue.position, requests.created_at, requests.rowid"""
        ).fetchall()
    return [dict(row) for row in rows]


def party_snapshot() -> dict:
    with connection() as database:
        invitation = invitation_state(database)
    return {"type": "requests", "items": request_snapshot(), "invitation": invitation}


async def watch_requests() -> None:
    previous = party_snapshot()
    while True:
        await asyncio.sleep(1)
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            expire_invitation(database, time.time())
        if not subscribers:
            previous = party_snapshot()
            continue
        current = party_snapshot()
        if current != previous:
            previous = current
            for subscriber in tuple(subscribers):
                try:
                    await subscriber.send_json({"type": "requests", "items": current})
                except (WebSocketDisconnect, RuntimeError, OSError):
                    subscribers.discard(subscriber)


@app.websocket("/ws/requests")
async def requests_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        token = message.get("token") if isinstance(message, dict) else None
        if not isinstance(token, str) or client_for_token(token) is None:
            await websocket.close(code=1008)
            return
        await websocket.send_json(party_snapshot())
        subscribers.add(websocket)
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        subscribers.discard(websocket)



@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/cantor", include_in_schema=False)
def singer_page() -> FileResponse:
    return FileResponse(SINGER_ROOT / "index.html")


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
    video_id = youtube_id(payload.youtubeCode)
    request_id = str(uuid4())
    with connection() as database:
        database.execute(
            "INSERT INTO requests (id, client_id, video_id) VALUES (?, ?, ?)",
            (request_id, client_id, video_id),
        )
    return {"id": request_id, "status": "pending", "video_id": video_id}


@app.get("/api/requests")
def list_requests(client_id: str = Depends(authenticated_client)) -> list[dict]:
    return request_snapshot()


@app.post("/api/requests/{request_id}/accept")
def accept_request(request_id: str, client_id: str = Depends(authenticated_client)) -> dict[str, str]:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        now = time.time()
        expire_invitation(database, now)
        if not accept_invitation(database, request_id, client_id, now):
            raise HTTPException(409, "Convite indisponível, vencido ou de outra pessoa")
    return {"status": "accepted"}


@app.get("/api/requests/{request_id}/preview")
def request_preview(request_id: str, client_id: str = Depends(authenticated_client)) -> FileResponse:
    with connection() as database:
        request = database.execute(
            "SELECT video_id FROM requests WHERE id = ? AND status = 'ready'", (request_id,)
        ).fetchone()
    if request is None:
        raise HTTPException(404, "Prévia indisponível")
    preview = MEDIA_ROOT / "previews" / f"{request['video_id']}.jpg"
    if not preview.is_file():
        raise HTTPException(404, "Prévia indisponível")
    return FileResponse(preview, media_type="image/jpeg")