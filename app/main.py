import asyncio
import hashlib
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
import shutil
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import qrcode

from app.invitations import accept_invitation, apply_skip, finish_song, invitation_state, schedule_skip, skip_state, start_invitation
from app.media import MEDIA_ROOT
from app.queue import enqueue_request
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


@app.middleware("http")
async def identify_party(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        with connection() as database:
            response.headers["X-Karaoke-Party"] = database.execute(
                "SELECT generation FROM party WHERE id = 1"
            ).fetchone()[0]
    return response


SINGER_ROOT = Path(__file__).resolve().parent.parent / "app-singer"
TV_ROOT = Path(__file__).resolve().parent.parent / "app-tv"
app.mount("/cantor/assets", StaticFiles(directory=SINGER_ROOT), name="cantor-assets")
if os.getenv("KARAOKE_TV_LOCAL") == "true":
    app.mount("/tv/assets", StaticFiles(directory=TV_ROOT), name="tv-assets")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
subscribers: set[WebSocket] = set()
tv_subscribers: set[WebSocket] = set()
singer_subscribers: dict[WebSocket, str] = {}


class NewSinger(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class NewRequest(BaseModel):
    youtubeCode: str = Field(min_length=11, max_length=11)


def authenticated_tv() -> None:
    if os.getenv("KARAOKE_TV_LOCAL") != "true":
        raise HTTPException(404, "TV indisponível")


def tv_command(request: Request, marker: str | None = Header(default=None, alias="X-Karaoke-TV")) -> None:
    authenticated_tv()
    origin = request.headers.get("origin")
    if marker != "local" or (origin and origin != str(request.base_url).rstrip("/")):
        raise HTTPException(403, "Comando disponível somente na TV local")


def youtube_id(code: str) -> str:
    if not VIDEO_ID.fullmatch(code):
        raise HTTPException(422, "Código de vídeo do YouTube inválido")
    return code


def singer_for_token(token: str) -> str | None:
    session_hash = hashlib.sha256(token.encode()).hexdigest()
    with connection() as database:
        singer = database.execute("SELECT id FROM singers WHERE session_hash = ?", (session_hash,)).fetchone()
    return singer["id"] if singer else None


def authenticated_singer(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sessão necessária")
    singer_id = singer_for_token(authorization[7:])
    if singer_id is None:
        raise HTTPException(401, "Sessão inválida")
    return singer_id


def request_snapshot() -> list[dict]:
    with connection() as database:
        rows = database.execute(
                        """SELECT requests.id, requests.singer_id, requests.video_id, requests.status, requests.title,
                                 requests.error, requests.created_at, singers.name AS singer_name,
                 ready_queue.position AS position
                             FROM requests JOIN singers ON singers.id = requests.singer_id
             LEFT JOIN ready_queue ON ready_queue.request_id = requests.id
             WHERE requests.status NOT IN ('removed', 'skipped', 'played')
                 ORDER BY CASE WHEN ready_queue.position IS NOT NULL
                     THEN 0 ELSE 1 END,
                   ready_queue.position, requests.created_at, requests.rowid"""
        ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["backvocals"] = [dict(backvocal) for backvocal in database.execute(
                """SELECT backvocals.singer_id, singers.name AS singer_name, backvocals.accepted,
                          backvocals.joined, backvocals.score_eligible
                   FROM backvocals JOIN singers ON singers.id = backvocals.singer_id
                   WHERE backvocals.request_id = ? ORDER BY backvocals.rowid""",
                (item["id"],),
            )]
    return items


def party_snapshot() -> dict:
    with connection() as database:
        invitation = invitation_state(database)
        skipping = skip_state(database)
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        singers = [dict(row) for row in database.execute("SELECT id, name FROM singers ORDER BY name, id")]
    return {"type": "requests", "items": request_snapshot(), "invitation": invitation,
            "skip": skipping, "allow_skip": skip_enabled(), "party": generation, "singers": singers}


def skip_enabled() -> bool:
    return os.getenv("KARAOKE_ALLOW_SKIP", "true").lower() in {"true", "1", "yes"}


async def watch_requests() -> None:
    previous = party_snapshot()
    while True:
        await asyncio.sleep(1)
        with connection() as database:
            database.execute("BEGIN IMMEDIATE")
            apply_skip(database, time.time())
            if tv_subscribers:
                start_invitation(database, time.time())
        if not subscribers:
            previous = party_snapshot()
            continue
        current = party_snapshot()
        if current != previous:
            previous = current
            for subscriber in tuple(subscribers):
                try:
                    if singer_subscribers.get(subscriber) not in (None, current["party"]):
                        await subscriber.close(code=1008)
                        subscribers.discard(subscriber)
                        singer_subscribers.pop(subscriber, None)
                        continue
                    await subscriber.send_json(current)
                except (WebSocketDisconnect, RuntimeError, OSError):
                    subscribers.discard(subscriber)
                    singer_subscribers.pop(subscriber, None)


@app.websocket("/ws/requests")
async def requests_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        token = message.get("token") if isinstance(message, dict) else None
        is_tv = os.getenv("KARAOKE_TV_LOCAL") == "true"
        if is_tv and websocket.headers.get("origin") != f"http://{websocket.headers.get('host')}":
            await websocket.close(code=1008)
            return
        if not is_tv and (not isinstance(token, str) or singer_for_token(token) is None):
            await websocket.close(code=1008)
            return
        if is_tv:
            tv_subscribers.add(websocket)
            with connection() as database:
                database.execute("BEGIN IMMEDIATE")
                start_invitation(database, time.time())
        else:
            with connection() as database:
                singer_subscribers[websocket] = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        await websocket.send_json(party_snapshot())
        subscribers.add(websocket)
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        subscribers.discard(websocket)
        tv_subscribers.discard(websocket)
        singer_subscribers.pop(websocket, None)



@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/cantor", include_in_schema=False)
def singer_page() -> FileResponse:
    return FileResponse(SINGER_ROOT / "index.html")


@app.get("/tv", include_in_schema=False)
def tv_page(_: None = Depends(authenticated_tv)) -> FileResponse:
    return FileResponse(TV_ROOT / "index.html")


@app.get("/api/tv/state", dependencies=[Depends(authenticated_tv)])
def tv_state() -> dict:
    return party_snapshot()


@app.post("/api/tv/reset", dependencies=[Depends(tv_command)])
def tv_reset() -> dict[str, str]:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        previous = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        generation = str(uuid4())
        database.execute("UPDATE party SET generation=? WHERE id=1", (generation,))
        for table in ("skip_request", "invitation", "missed_invitations", "ready_queue",
                      "accepted_counts", "requests", "singers"):
            database.execute(f"DELETE FROM {table}")
    shutil.rmtree(MEDIA_ROOT / previous, ignore_errors=True)
    for directory in (MEDIA_ROOT / "videos", MEDIA_ROOT / "previews"):
        shutil.rmtree(directory, ignore_errors=True)
    return {"party": generation}


@app.post("/api/tv/start", dependencies=[Depends(tv_command)])
def tv_start() -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        invitation = start_invitation(database, time.time())
    return {"invitation": invitation}


@app.post("/api/tv/{request_id}/finish", dependencies=[Depends(tv_command)])
def tv_finish(request_id: str) -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        if not finish_song(database, request_id, time.time()):
            raise HTTPException(409, "Música não está em apresentação")
    return {"status": "played"}


@app.get("/api/tv/{request_id}/{kind}", dependencies=[Depends(authenticated_tv)])
def tv_media(request_id: str, kind: str) -> FileResponse:
    if kind not in {"video", "preview"}:
        raise HTTPException(404, "Mídia indisponível")
    with connection() as database:
        item = database.execute("SELECT video_id FROM requests WHERE id = ? AND status = 'ready'", (request_id,)).fetchone()
    if item is None:
        raise HTTPException(404, "Mídia indisponível")
    with connection() as database:
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
    media = MEDIA_ROOT / generation / ("videos" if kind == "video" else "previews") / (
        item["video_id"] + (".mp4" if kind == "video" else ".jpg")
    )
    if not media.is_file():
        raise HTTPException(404, "Mídia indisponível")
    return FileResponse(media, media_type="video/mp4" if kind == "video" else "image/jpeg")


def singer_address(request: Request) -> str:
    address = os.getenv("KARAOKE_LAN_IP", "")
    if not address:
        raise HTTPException(503, "Inicie com start.ps1 para detectar o IP da rede")
    return f"http://{address}:8000/cantor"


@app.get("/api/tv/join", dependencies=[Depends(authenticated_tv)])
def tv_join(request: Request) -> dict[str, str]:
    return {"url": singer_address(request)}


@app.get("/api/tv/qr", dependencies=[Depends(authenticated_tv)])
def tv_qr(request: Request) -> StreamingResponse:
    image = qrcode.make(singer_address(request))
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return StreamingResponse(output, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/singers", status_code=201)
def create_singer(payload: NewSinger) -> dict[str, str]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Informe seu nome")
    token = secrets.token_urlsafe(32)
    session_hash = hashlib.sha256(token.encode()).hexdigest()
    normalized_name = " ".join(name.split()).casefold()
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        party = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        matches = [row for row in database.execute("SELECT id, name FROM singers ORDER BY created_at, rowid")
                   if " ".join(row["name"].split()).casefold() == normalized_name]
        if matches:
            singer_id = matches[0]["id"]
            name = matches[0]["name"]
            duplicates = [row["id"] for row in matches[1:]]
            if duplicates:
                database.executemany(
                    "UPDATE requests SET singer_id = ? WHERE singer_id = ?",
                    [(singer_id, duplicate_id) for duplicate_id in duplicates],
                )
                for duplicate_id in duplicates:
                    vocals = database.execute(
                        "SELECT request_id, accepted, joined, score_eligible FROM backvocals WHERE singer_id = ?",
                        (duplicate_id,),
                    ).fetchall()
                    for vocal in vocals:
                        owner = database.execute(
                            "SELECT singer_id FROM requests WHERE id = ?", (vocal["request_id"],)
                        ).fetchone()
                        if owner and owner["singer_id"] == singer_id:
                            database.execute(
                                "DELETE FROM backvocals WHERE request_id = ? AND singer_id = ?",
                                (vocal["request_id"], duplicate_id),
                            )
                            continue
                        existing = database.execute(
                            "SELECT accepted, joined, score_eligible FROM backvocals WHERE request_id = ? AND singer_id = ?",
                            (vocal["request_id"], singer_id),
                        ).fetchone()
                        if existing:
                            joined = 1 if 1 in (existing["joined"], vocal["joined"]) else (
                                0 if 0 in (existing["joined"], vocal["joined"]) else -1
                            )
                            database.execute(
                                "UPDATE backvocals SET accepted = ?, joined = ?, score_eligible = ? WHERE request_id = ? AND singer_id = ?",
                                (max(existing["accepted"], vocal["accepted"]), joined,
                                 max(existing["score_eligible"], vocal["score_eligible"]), vocal["request_id"], singer_id),
                            )
                            database.execute(
                                "DELETE FROM backvocals WHERE request_id = ? AND singer_id = ?",
                                (vocal["request_id"], duplicate_id),
                            )
                        else:
                            database.execute(
                                "UPDATE backvocals SET singer_id = ? WHERE request_id = ? AND singer_id = ?",
                                (singer_id, vocal["request_id"], duplicate_id),
                            )
                accepted_total = database.execute(
                    "SELECT COALESCE(SUM(total), 0) FROM accepted_counts WHERE singer_id IN (?, {})".format(
                        ",".join("?" for _ in duplicates)
                    ), (singer_id, *duplicates),
                ).fetchone()[0]
                count_exists = database.execute(
                    "SELECT 1 FROM accepted_counts WHERE singer_id IN ({}) LIMIT 1".format(
                        ",".join("?" for _ in [singer_id, *duplicates])
                    ), (singer_id, *duplicates),
                ).fetchone()
                if count_exists:
                    database.execute(
                        "INSERT INTO accepted_counts(singer_id, total) VALUES (?, ?) "
                        "ON CONFLICT(singer_id) DO UPDATE SET total = excluded.total",
                        (singer_id, accepted_total),
                    )
                database.executemany("DELETE FROM singers WHERE id = ?", [(duplicate_id,) for duplicate_id in duplicates])
            database.execute("UPDATE singers SET session_hash = ? WHERE id = ?", (session_hash, singer_id))
        else:
            singer_id = str(uuid4())
            database.execute(
                "INSERT INTO singers (id, name, session_hash) VALUES (?, ?, ?)",
                (singer_id, name, session_hash),
            )
    return {"singer_id": singer_id, "name": name, "token": token, "party": party}


@app.post("/api/requests", status_code=201)
def create_request(payload: NewRequest, singer_id: str = Depends(authenticated_singer)) -> dict[str, str]:
    video_id = youtube_id(payload.youtubeCode)
    request_id = str(uuid4())
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            """SELECT id, singer_id, status FROM requests WHERE video_id = ?
               AND status IN ('pending', 'processing', 'ready') ORDER BY created_at, rowid LIMIT 1""",
            (video_id,),
        ).fetchone()
        if existing:
            if singer_id == existing["singer_id"] or database.execute(
                "SELECT 1 FROM backvocals WHERE request_id = ? AND singer_id = ?",
                (existing["id"], singer_id),
            ).fetchone():
                raise HTTPException(409, "Você já está nesta música")
            invited = invitation_state(database)
            if invited and invited["request_id"] == existing["id"] and (
                invited["accepted"] or invited["lead_accepted"] or
                database.execute("SELECT 1 FROM backvocals WHERE request_id = ? AND accepted = 1",
                                 (existing["id"],)).fetchone()
            ):
                raise HTTPException(409, "A música já foi aceita para apresentação")
            database.execute("INSERT INTO backvocals(request_id, singer_id) VALUES (?, ?)",
                             (existing["id"], singer_id))
            return {"id": existing["id"], "status": existing["status"], "video_id": video_id}
        database.execute(
            "INSERT INTO requests (id, singer_id, video_id) VALUES (?, ?, ?)",
            (request_id, singer_id, video_id),
        )
        enqueue_request(database, request_id)
    return {"id": request_id, "status": "pending", "video_id": video_id}


@app.get("/api/requests")
def list_requests(singer_id: str = Depends(authenticated_singer)) -> list[dict]:
    return request_snapshot()


@app.post("/api/requests/{request_id}/invite/{guest_id}", status_code=202)
def invite_guest(request_id: str, guest_id: str, singer_id: str = Depends(authenticated_singer)) -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        request = database.execute("SELECT singer_id, status FROM requests WHERE id = ?", (request_id,)).fetchone()
        invitation = invitation_state(database)
        if (not request or request["singer_id"] != singer_id or request["status"] != "ready"
                or not invitation or invitation["request_id"] != request_id or invitation["accepted"]
                or invitation["lead_accepted"] or skip_state(database)):
            raise HTTPException(409, "Convite indisponível")
        if guest_id == singer_id or not database.execute("SELECT 1 FROM singers WHERE id = ?", (guest_id,)).fetchone():
            raise HTTPException(404, "Pessoa não encontrada")
        if database.execute("SELECT 1 FROM backvocals WHERE request_id = ? AND singer_id = ?", (request_id, guest_id)).fetchone():
            raise HTTPException(409, "Pessoa já convidada")
        database.execute("INSERT INTO backvocals(request_id, singer_id, joined) VALUES (?, ?, 0)", (request_id, guest_id))
    return {"status": "pending"}


@app.post("/api/requests/{request_id}/invite/respond")
def respond_to_invite(request_id: str, accepted: bool, singer_id: str = Depends(authenticated_singer)) -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        invitation = invitation_state(database)
        if not invitation or invitation["request_id"] != request_id or skip_state(database):
            raise HTTPException(409, "Convite indisponível")
        result = database.execute(
            "UPDATE backvocals SET joined = ?, score_eligible = ? WHERE request_id = ? AND singer_id = ? AND joined = 0",
            (1 if accepted else -1, int(accepted and not invitation["accepted"]), request_id, singer_id),
        )
        if not result.rowcount:
            raise HTTPException(409, "Convite já respondido")
        if invitation["lead_accepted"] and not database.execute(
            "SELECT 1 FROM backvocals WHERE request_id = ? AND joined >= 0 AND accepted = 0", (request_id,),
        ).fetchone():
            database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
    return {"status": "accepted" if accepted else "declined"}


@app.post("/api/requests/{request_id}/accept")
def accept_request(request_id: str, singer_id: str = Depends(authenticated_singer)) -> dict[str, str]:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        now = time.time()
        if not accept_invitation(database, request_id, singer_id, now):
            raise HTTPException(409, "Convite indisponível ou de outra pessoa")
    return {"status": "accepted"}


@app.post("/api/requests/{request_id}/skip", status_code=202)
def skip_request(request_id: str, singer_id: str = Depends(authenticated_singer)) -> dict:
    if not skip_enabled():
        raise HTTPException(403, "Pular música está desativado")
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        skipping = schedule_skip(database, request_id, time.time())
        if skipping is None:
            raise HTTPException(409, "Esta música não está em uso ou já está sendo pulada")
    return skipping


@app.delete("/api/requests/{request_id}", status_code=204)
def remove_request(request_id: str, singer_id: str = Depends(authenticated_singer)) -> None:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        request = database.execute(
            "SELECT status, singer_id FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if request is None or request["status"] == "removed":
            raise HTTPException(404, "Pedido não encontrado")
        invite = invitation_state(database)
        backvocal = database.execute(
            "SELECT accepted FROM backvocals WHERE request_id = ? AND singer_id = ?", (request_id, singer_id)
        ).fetchone()
        if request["singer_id"] != singer_id and backvocal is None:
            raise HTTPException(404, "Pedido não encontrado")
        invited = invite and invite["request_id"] == request_id
        if request["status"] not in ("pending", "processing", "ready", "failed") or (
            invited and (invite["accepted"] or (backvocal["accepted"] if backvocal else invite["lead_accepted"]))
        ):
            raise HTTPException(409, "Não é possível remover uma música já aceita")
        if backvocal is not None:
            database.execute("DELETE FROM backvocals WHERE request_id = ? AND singer_id = ?", (request_id, singer_id))
            if invited and invite["lead_accepted"] and not database.execute(
                "SELECT 1 FROM backvocals WHERE request_id = ? AND joined = 1 AND accepted = 0 LIMIT 1", (request_id,)
            ).fetchone():
                database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
            return
        database.execute("UPDATE requests SET status = 'removed' WHERE id = ?", (request_id,))
        database.execute("DELETE FROM ready_queue WHERE request_id = ?", (request_id,))
        rows = database.execute("SELECT request_id FROM ready_queue ORDER BY position").fetchall()
        for position, row in enumerate(rows, start=1):
            database.execute("UPDATE ready_queue SET position = ? WHERE request_id = ?", (position, row["request_id"]))
        if invite and invite["request_id"] == request_id:
            database.execute("DELETE FROM skip_request WHERE id = 1")
            database.execute("DELETE FROM invitation WHERE id = 1")
            start_invitation(database, time.time())


@app.get("/api/requests/{request_id}/preview")
def request_preview(request_id: str, singer_id: str = Depends(authenticated_singer)) -> FileResponse:
    with connection() as database:
        request = database.execute(
            "SELECT video_id FROM requests WHERE id = ? AND status = 'ready'", (request_id,)
        ).fetchone()
    if request is None:
        raise HTTPException(404, "Prévia indisponível")
    with connection() as database:
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
    preview = MEDIA_ROOT / generation / "previews" / f"{request['video_id']}.jpg"
    if not preview.is_file():
        raise HTTPException(404, "Prévia indisponível")
    return FileResponse(preview, media_type="image/jpeg")