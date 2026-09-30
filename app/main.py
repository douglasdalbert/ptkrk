import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
import shutil
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import qrcode

from app.invitations import accept_invitation, apply_skip, finish_song, invitation_state, schedule_skip, skip_state, start_invitation
from app.scoring import (
    max_score,
    microphone_rms_threshold,
    off_cue_rearm_ms,
    onset_tolerance_ms,
    record_block_result,
    record_onset,
    score_snapshot,
)
from app.media import MEDIA_ROOT, remove_unused_media
from app.queue import enqueue_request
from app.storage import (
    add_vocal_activity,
    add_tv_notification,
    connection,
    initialize,
    latest_vocal_activity_id,
    latest_tv_notification_id,
    load_playback_sync,
    save_playback_sync,
    tv_notifications_since,
    vocal_activity_since,
)


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
app.mount("/assets", StaticFiles(directory=SINGER_ROOT / "assets"), name="shared-assets")
app.mount("/cantor/assets", StaticFiles(directory=SINGER_ROOT), name="cantor-assets")
if os.getenv("KARAOKE_TV_LOCAL") == "true":
    app.mount("/tv/assets", StaticFiles(directory=TV_ROOT), name="tv-assets")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
subscribers: set[WebSocket] = set()
tv_subscribers: set[WebSocket] = set()
singer_subscribers: dict[WebSocket, str] = {}
singer_socket_ids: dict[WebSocket, str] = {}
group_room_subscribers: dict[str, set[WebSocket]] = {}
playback_sync: dict | None = None


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


def party_snapshot(include_caption_bars: bool = False) -> dict:
    with connection() as database:
        invitation = invitation_state(database)
        skipping = skip_state(database)
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        singers = [dict(row) for row in database.execute("SELECT id, name FROM singers ORDER BY name, id")]
        scores = []
        if invitation:
            generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
            current_video = database.execute(
                "SELECT video_id FROM requests WHERE id = ?", (invitation["request_id"],)
            ).fetchone()
            captions = MEDIA_ROOT / generation / "captions" / f"{current_video['video_id']}.json" if current_video else None
            if include_caption_bars and captions and captions.is_file():
                try:
                    active_bars = json.loads(captions.read_text(encoding="utf-8")).get("bars", [])
                except (OSError, json.JSONDecodeError):
                    active_bars = []
            else:
                active_bars = []
            scores = score_snapshot(database, invitation["request_id"], captions)
        else:
            active_bars = []
    snapshot = {"type": "requests", "items": request_snapshot(), "invitation": invitation,
                "skip": skipping, "allow_skip": skip_enabled(), "party": generation, "singers": singers,
                "scores": scores, "ranking_max": max_score(),
                "score_tolerance_ms": onset_tolerance_ms(),
                "off_cue_rearm_ms": off_cue_rearm_ms(),
                "microphone_rms_threshold": microphone_rms_threshold()}
    if include_caption_bars:
        snapshot["active_bars"] = active_bars
    return snapshot


def group_room_snapshot(request_id: str) -> dict | None:
    with connection() as database:
        room = database.execute(
            """SELECT group_rooms.video_id, requests.video_id AS request_video_id
               FROM group_rooms JOIN requests ON requests.id = group_rooms.request_id
               WHERE group_rooms.request_id = ?""", (request_id,),
        ).fetchone()
        if room is None or room["video_id"] != room["request_video_id"]:
            return None
        members = [dict(row) for row in database.execute(
            """SELECT backvocals.singer_id, singers.name AS singer_name, backvocals.joined,
                      backvocals.accepted, backvocals.score_eligible
               FROM backvocals JOIN singers ON singers.id = backvocals.singer_id
               WHERE backvocals.request_id = ? ORDER BY backvocals.rowid""",
            (request_id,),
        )]
    return {"type": "group_state", "request_id": request_id, "video_id": room["video_id"],
            "members": members}


def can_join_group_room(request_id: str, singer_id: str) -> bool:
    with connection() as database:
        return database.execute(
            """SELECT 1 FROM group_rooms JOIN requests ON requests.id = group_rooms.request_id
               WHERE group_rooms.request_id = ? AND group_rooms.video_id = requests.video_id
                     AND requests.singer_id = ?""", (request_id, singer_id),
        ).fetchone() is not None


async def publish_group_room(request_id: str) -> None:
    message = group_room_snapshot(request_id)
    if message is None:
        return
    room_subscribers = group_room_subscribers.get(request_id, set())
    for subscriber in tuple(room_subscribers):
        try:
            await subscriber.send_json(message)
        except (WebSocketDisconnect, RuntimeError, OSError):
            room_subscribers.discard(subscriber)
    if not room_subscribers:
        group_room_subscribers.pop(request_id, None)


def skip_enabled() -> bool:
    return os.getenv("KARAOKE_ALLOW_SKIP", "true").lower() in {"true", "1", "yes"}


async def watch_requests() -> None:
    previous = party_snapshot()
    last_activity_id = latest_vocal_activity_id()
    last_notification_id = latest_tv_notification_id()
    last_sync_time_ms = 0
    next_snapshot_check = time.monotonic()
    while True:
        await asyncio.sleep(0.05)
        sync = load_playback_sync()
        if sync and sync["server_time_ms"] != last_sync_time_ms:
            last_sync_time_ms = sync["server_time_ms"]
            for subscriber in tuple(subscribers - tv_subscribers):
                try:
                    await subscriber.send_json(sync)
                except (WebSocketDisconnect, RuntimeError, OSError):
                    subscribers.discard(subscriber)
                    singer_subscribers.pop(subscriber, None)
                    singer_socket_ids.pop(subscriber, None)
        if tv_subscribers:
            for activity in vocal_activity_since(last_activity_id):
                last_activity_id = activity["id"]
                message = {
                    "type": "vocal_activity",
                    "request_id": activity["request_id"],
                    "singer_id": activity["singer_id"],
                    "speaking": bool(activity["speaking"]),
                    "position_ms": activity["position_ms"],
                    "state_since_ms": activity["state_since_ms"],
                }
                for subscriber in tuple(tv_subscribers):
                    try:
                        await subscriber.send_json(message)
                    except (WebSocketDisconnect, RuntimeError, OSError):
                        subscribers.discard(subscriber)
                        tv_subscribers.discard(subscriber)
            for notification in tv_notifications_since(last_notification_id):
                last_notification_id = notification["id"]
                message = json.loads(notification["payload"])
                for subscriber in tuple(tv_subscribers):
                    try:
                        await subscriber.send_json(message)
                    except (WebSocketDisconnect, RuntimeError, OSError):
                        subscribers.discard(subscriber)
                        tv_subscribers.discard(subscriber)
        if time.monotonic() < next_snapshot_check:
            continue
        next_snapshot_check = time.monotonic() + 1
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
                    await subscriber.send_json(
                        party_snapshot(include_caption_bars=True)
                        if subscriber in tv_subscribers else current
                    )
                except (WebSocketDisconnect, RuntimeError, OSError):
                    subscribers.discard(subscriber)
                    singer_subscribers.pop(subscriber, None)


@app.websocket("/ws/requests")
async def requests_socket(websocket: WebSocket) -> None:
    global playback_sync
    await websocket.accept()
    singer_id = None
    is_tv = False
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        token = message.get("token") if isinstance(message, dict) else None
        is_tv = os.getenv("KARAOKE_TV_LOCAL") == "true"
        if is_tv and websocket.headers.get("origin") != f"http://{websocket.headers.get('host')}":
            await websocket.close(code=1008)
            return
        if not is_tv:
            singer_id = singer_for_token(token) if isinstance(token, str) else None
            if singer_id is None:
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
                singer_socket_ids[websocket] = singer_id
        await websocket.send_json(party_snapshot(include_caption_bars=is_tv))
        sync = load_playback_sync() if not is_tv else None
        if sync and time.time() - sync["server_time"] < 1:
            await websocket.send_json({**sync, "type": "playback_sync"})
        subscribers.add(websocket)
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            if is_tv and message.get("type") == "playback_sync":
                request_id = message.get("request_id")
                position_ms = message.get("position_ms")
                if not isinstance(request_id, str) or not isinstance(position_ms, int) or position_ms < 0:
                    continue
                with connection() as database:
                    invitation = invitation_state(database)
                if not invitation or invitation["request_id"] != request_id or not invitation["accepted"]:
                    playback_sync = None
                    save_playback_sync(None)
                    continue
                playback_sync = {
                    "type": "playback_sync",
                    "request_id": request_id,
                    "position_ms": position_ms,
                    "playing": bool(message.get("playing")),
                    "server_time": time.time(),
                    "server_time_ms": round(time.time() * 1000),
                }
                save_playback_sync(playback_sync)
                continue
            if is_tv and message.get("type") == "block_result":
                request_id = message.get("request_id")
                singer_id = message.get("singer_id")
                event_id = message.get("event_id")
                block_index = message.get("block_index")
                hit = message.get("hit")
                if (not isinstance(request_id, str) or not isinstance(singer_id, str)
                        or not isinstance(event_id, str) or len(event_id) > 80
                        or not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 0
                        or not isinstance(hit, bool)):
                    continue
                with connection() as database:
                    invitation = invitation_state(database)
                    if not invitation or invitation["request_id"] != request_id or not invitation["accepted"]:
                        continue
                    owner = database.execute(
                        "SELECT singer_id, video_id FROM requests WHERE id = ?", (request_id,)
                    ).fetchone()
                    if owner is None:
                        continue
                    eligible = owner["singer_id"] == singer_id and invitation["lead_accepted"]
                    if not eligible:
                        eligible = database.execute(
                            "SELECT 1 FROM backvocals WHERE request_id = ? AND singer_id = ? "
                            "AND joined = 1 AND accepted = 1 AND score_eligible = 1",
                            (request_id, singer_id),
                        ).fetchone() is not None
                    if not eligible:
                        continue
                    generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
                    caption_path = MEDIA_ROOT / generation / "captions" / f"{owner['video_id']}.json"
                    database.execute("BEGIN IMMEDIATE")
                    update = record_block_result(
                        database, request_id, singer_id, event_id, block_index, hit, caption_path
                    )
                if update:
                    await websocket.send_json(update)
                continue
            if is_tv:
                continue
            if message.get("type") == "microphone_activity":
                request_id = message.get("request_id")
                speaking = message.get("speaking")
                event_id = message.get("event_id")
                position_ms = message.get("position_ms")
                state_since_ms = message.get("state_since_ms", position_ms)
                if (not isinstance(request_id, str) or not isinstance(speaking, bool)
                        or not isinstance(event_id, str) or len(event_id) > 80
                        or (position_ms is not None and
                            (not isinstance(position_ms, int) or isinstance(position_ms, bool)
                             or not 0 <= position_ms <= 12 * 60 * 1000))
                        or (state_since_ms is not None and
                            (not isinstance(state_since_ms, int) or isinstance(state_since_ms, bool)
                             or not 0 <= state_since_ms <= 12 * 60 * 1000))):
                    continue
                with connection() as database:
                    invitation = invitation_state(database)
                    if not invitation or invitation["request_id"] != request_id or not invitation["accepted"]:
                        continue
                    owner = database.execute(
                        "SELECT singer_id FROM requests WHERE id = ?", (request_id,)
                    ).fetchone()
                    if owner is None:
                        continue
                    singer_id = singer_socket_ids.get(websocket)
                    eligible = owner["singer_id"] == singer_id and invitation["lead_accepted"]
                    if not eligible:
                        eligible = database.execute(
                            "SELECT 1 FROM backvocals WHERE request_id = ? AND singer_id = ? "
                            "AND joined = 1 AND accepted = 1 AND score_eligible = 1",
                            (request_id, singer_id),
                        ).fetchone() is not None
                    if not eligible:
                        continue
                add_vocal_activity(
                    event_id, request_id, singer_id, speaking, position_ms, state_since_ms, time.time()
                )
                continue
            if message.get("type") == "vocal_onset":
                request_id = message.get("request_id")
                position_ms = message.get("position_ms")
                event_id = message.get("event_id")
                reported_block_index = message.get("block_index")
                if (not isinstance(request_id, str) or not isinstance(position_ms, int)
                    or not isinstance(event_id, str) or len(event_id) > 80
                    or (reported_block_index is not None and
                        (not isinstance(reported_block_index, int) or isinstance(reported_block_index, bool)
                         or reported_block_index < 0))):
                    continue
                if not 0 <= position_ms <= 12 * 60 * 1000:
                    continue
                sync = load_playback_sync()
                if not sync or sync["request_id"] != request_id or time.time() - sync["server_time"] > 0.75:
                    continue
                expected_position = sync["position_ms"] + (
                    round((time.time() - sync["server_time"]) * 1000) if sync["playing"] else 0
                )
                if abs(position_ms - expected_position) > 300:
                    continue
                with connection() as database:
                    invitation = invitation_state(database)
                    if not invitation or invitation["request_id"] != request_id or not invitation["accepted"]:
                        continue
                    owner = database.execute(
                        "SELECT singer_id, video_id FROM requests WHERE id = ?", (request_id,)
                    ).fetchone()
                    if owner is None:
                        continue
                    singer_id = singer_socket_ids.get(websocket)
                    eligible = owner["singer_id"] == singer_id and invitation["lead_accepted"]
                    if not eligible:
                        eligible = database.execute(
                            "SELECT 1 FROM backvocals WHERE request_id = ? AND singer_id = ? "
                            "AND joined = 1 AND accepted = 1 AND score_eligible = 1",
                            (request_id, singer_id),
                        ).fetchone() is not None
                    if not eligible:
                        continue
                    generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
                    caption_path = MEDIA_ROOT / generation / "captions" / f"{owner['video_id']}.json"
                    database.execute("BEGIN IMMEDIATE")
                    update = record_onset(
                        database, request_id, singer_id, event_id, position_ms, caption_path
                    )
                if update:
                    update["reported_block_index"] = reported_block_index
                    update["client_match_verified"] = update["block_index"] == reported_block_index
                    add_tv_notification(f"score:{event_id}", request_id, update)
                    for subscriber in tuple(subscribers):
                        if subscriber in tv_subscribers:
                            continue
                        try:
                            await subscriber.send_json(update)
                        except (WebSocketDisconnect, RuntimeError, OSError):
                            subscribers.discard(subscriber)
                continue
            request_id = message.get("request_id")
            if not isinstance(request_id, str):
                continue
            if message.get("type") == "join_group" and can_join_group_room(request_id, singer_id):
                group_room_subscribers.setdefault(request_id, set()).add(websocket)
                state = group_room_snapshot(request_id)
                if state is not None:
                    await websocket.send_json(state)
            elif message.get("type") == "leave_group":
                room_subscribers = group_room_subscribers.get(request_id)
                if room_subscribers:
                    room_subscribers.discard(websocket)
                    if not room_subscribers:
                        group_room_subscribers.pop(request_id, None)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        subscribers.discard(websocket)
        tv_subscribers.discard(websocket)
        singer_subscribers.pop(websocket, None)
        singer_socket_ids.pop(websocket, None)
        for request_id, room_subscribers in tuple(group_room_subscribers.items()):
            room_subscribers.discard(websocket)
            if not room_subscribers:
                group_room_subscribers.pop(request_id, None)



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
    global playback_sync
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        previous = database.execute("SELECT generation FROM party WHERE id=1").fetchone()[0]
        generation = str(uuid4())
        database.execute("UPDATE party SET generation=? WHERE id=1", (generation,))
        for table in ("skip_request", "invitation", "missed_invitations", "group_rooms", "ready_queue",
                      "accepted_counts", "requests", "singers", "vocal_activity", "tv_notifications",
                      "runtime_state"):
            database.execute(f"DELETE FROM {table}")
    playback_sync = None
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
    if kind not in {"video", "preview", "captions"}:
        raise HTTPException(404, "Mídia indisponível")
    with connection() as database:
        item = database.execute("SELECT video_id FROM requests WHERE id = ? AND status = 'ready'", (request_id,)).fetchone()
    if item is None:
        raise HTTPException(404, "Mídia indisponível")
    with connection() as database:
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
    category, suffix = {
        "video": ("videos", ".mp4"),
        "preview": ("previews", ".jpg"),
        "captions": ("captions", ".json"),
    }[kind]
    media = MEDIA_ROOT / generation / category / f"{item['video_id']}{suffix}"
    if not media.is_file():
        raise HTTPException(404, "Mídia indisponível")
    media_type = {"video": "video/mp4", "preview": "image/jpeg", "captions": "application/json"}[kind]
    return FileResponse(media, media_type=media_type)


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


@app.post("/api/requests/{request_id}/invite/respond")
def respond_to_invite(request_id: str, accepted: bool, singer_id: str = Depends(authenticated_singer),
                      background_tasks: BackgroundTasks = None) -> dict:
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
    if background_tasks is not None:
        background_tasks.add_task(publish_group_room, request_id)
    return {"status": "accepted" if accepted else "declined"}


@app.post("/api/requests/{request_id}/group/open")
def open_group_room(request_id: str, singer_id: str = Depends(authenticated_singer)) -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        request = database.execute(
            "SELECT singer_id, video_id, status FROM requests WHERE id = ?", (request_id,),
        ).fetchone()
        invitation = invitation_state(database)
        if (not request or request["singer_id"] != singer_id or request["status"] != "ready"
                or not invitation or invitation["request_id"] != request_id or skip_state(database)):
            raise HTTPException(409, "Grupo indisponível")
        database.execute(
            "INSERT INTO group_rooms(request_id, video_id, opened_at) VALUES (?, ?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET video_id = excluded.video_id",
            (request_id, request["video_id"], time.time()),
        )
    return group_room_snapshot(request_id)


@app.get("/api/requests/{request_id}/group/state")
def get_group_room_state(request_id: str, singer_id: str = Depends(authenticated_singer)) -> dict:
    if not can_join_group_room(request_id, singer_id):
        raise HTTPException(404, "Grupo indisponível")
    state = group_room_snapshot(request_id)
    if state is None:
        raise HTTPException(404, "Grupo indisponível")
    return state


@app.post("/api/requests/{request_id}/invite/{guest_id}", status_code=202)
def invite_guest(request_id: str, guest_id: str, singer_id: str = Depends(authenticated_singer),
                 background_tasks: BackgroundTasks = None) -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        request = database.execute("SELECT singer_id, video_id, status FROM requests WHERE id = ?", (request_id,)).fetchone()
        invitation = invitation_state(database)
        if (not request or request["singer_id"] != singer_id or request["status"] != "ready"
                or not invitation or invitation["request_id"] != request_id or invitation["accepted"]
                or invitation["lead_accepted"] or skip_state(database)):
            raise HTTPException(409, "Convite indisponível")
        if guest_id == singer_id or not database.execute("SELECT 1 FROM singers WHERE id = ?", (guest_id,)).fetchone():
            raise HTTPException(404, "Pessoa não encontrada")
        if database.execute("SELECT 1 FROM backvocals WHERE request_id = ? AND singer_id = ?", (request_id, guest_id)).fetchone():
            raise HTTPException(409, "Pessoa já convidada")
        database.execute(
            "INSERT OR IGNORE INTO group_rooms(request_id, video_id, opened_at) VALUES (?, ?, ?)",
            (request_id, request["video_id"], time.time()),
        )
        database.execute("INSERT INTO backvocals(request_id, singer_id, joined) VALUES (?, ?, 0)", (request_id, guest_id))
    if background_tasks is not None:
        background_tasks.add_task(publish_group_room, request_id)
    return {"status": "pending"}


@app.post("/api/requests/{request_id}/accept")
def accept_request(request_id: str, singer_id: str = Depends(authenticated_singer)) -> dict:
    with connection() as database:
        database.execute("BEGIN IMMEDIATE")
        now = time.time()
        if not accept_invitation(database, request_id, singer_id, now):
            raise HTTPException(409, "Convite indisponível ou de outra pessoa")
        invitation = invitation_state(database)
    return {"status": "accepted", "invitation": invitation}


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
            "SELECT status, singer_id, video_id FROM requests WHERE id = ?", (request_id,)
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
            generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
            remove_unused_media(database, request["video_id"], generation)
            if invited and invite["lead_accepted"] and not database.execute(
                "SELECT 1 FROM backvocals WHERE request_id = ? AND joined = 1 AND accepted = 0 LIMIT 1", (request_id,)
            ).fetchone():
                database.execute("UPDATE invitation SET accepted = 1 WHERE id = 1")
            return
        database.execute("DELETE FROM group_rooms WHERE request_id = ?", (request_id,))
        database.execute("DELETE FROM backvocals WHERE request_id = ?", (request_id,))
        database.execute("UPDATE requests SET status = 'removed' WHERE id = ?", (request_id,))
        database.execute("DELETE FROM ready_queue WHERE request_id = ?", (request_id,))
        generation = database.execute("SELECT generation FROM party WHERE id = 1").fetchone()[0]
        remove_unused_media(database, request["video_id"], generation)
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