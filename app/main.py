from fastapi import FastAPI

app = FastAPI(title="AI Receptionist")

@app.get("/health")
async def health():
    return {"status": "ok"}
