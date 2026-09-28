from fastapi import FastAPI

app = FastAPI(title="Karaoke")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}