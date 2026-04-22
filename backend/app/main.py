from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, folders, labs, listing, system, users
from app.database import engine
from app.config import get_settings

settings = get_settings()
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
app.include_router(listing.router)
app.include_router(system.router)
app.include_router(users.router)


@app.on_event("startup")
async def startup():
    # Tables are managed by Alembic migrations.
    # Dev convenience: warn if DB looks empty.
    from sqlalchemy import text
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema='public' AND table_name='users')")
        )
        has_users = result.scalar()
        if not has_users:
            import logging
            logger = logging.getLogger("nova-ve")
            logger.warning(
                "Database tables not found. Run: cd backend && alembic upgrade head && python -m scripts.seed"
            )


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
