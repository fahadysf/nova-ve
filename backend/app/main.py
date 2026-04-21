from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, folders, labs, system, users
from app.database import Base, engine

app = FastAPI(title="nova-ve", version="0.1.0-alpha")

# CORS for frontend dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)
app.include_router(folders.router)
app.include_router(labs.router)
app.include_router(system.router)
app.include_router(users.router)


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.on_event("shutdown")
async def shutdown():
    await engine.dispose()


@app.get("/VERSION")
async def version():
    return "0.1.0-alpha"


@app.get("/local_version")
async def local_version():
    return "0.1.0-alpha"


@app.get("/online_version")
async def online_version():
    return "0.1.0-alpha"
