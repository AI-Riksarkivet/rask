from fastapi import FastAPI

app = FastAPI(title="fastapi-thing")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
