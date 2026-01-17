import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import (
    admin_router,
    ai_router,
    aquariums_router,
    auth_router,
    family_router,
    feeding_router,
    fish_router,
    gamification_router,
    health_router,
    purchase_router,
    push_router,
    species_admin_router,
    species_router,
    sync_router,
    users_router,
)
from app.database import get_db
from app.models import Base
from app.models.species import Species
from app.redis import get_redis

# Use test database URL, fallback to local PostgreSQL
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/fishfeed_test",
)

# Use test Redis URL, fallback to local Redis
TEST_REDIS_URL = os.getenv(
    "TEST_REDIS_URL",
    "redis://localhost:6379/1",
)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_engine():
    """Create async engine for testing with PostgreSQL."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Seed test species data
    async_session_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with async_session_maker() as session:
        test_species = [
            Species(
                id="test-guppy",
                common_name="Test Guppy",
                scientific_name="Poecilia reticulata",
                food_types=["flakes", "live"],
                feeding_frequency=2,
                care_level="beginner",
                water_type="freshwater",
            ),
            Species(
                id="test-betta",
                common_name="Test Betta",
                scientific_name="Betta splendens",
                food_types=["pellets", "live"],
                feeding_frequency=2,
                care_level="beginner",
                water_type="freshwater",
            ),
            Species(
                id="test-hungry",
                common_name="Test Hungry Fish",
                scientific_name="Hungrius maximus",
                food_types=["everything"],
                feeding_frequency=3,
                care_level="intermediate",
                water_type="freshwater",
            ),
        ]
        for sp in test_species:
            session.add(sp)
        await session.commit()

    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession]:
    """Create async session for each test."""
    async_session_maker = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with async_session_maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def redis_client() -> AsyncGenerator[Redis]:
    """Create Redis client for testing."""
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def app(async_engine, redis_client) -> AsyncGenerator[FastAPI]:
    """Create FastAPI app for testing."""
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(species_router)
    app.include_router(species_admin_router)
    app.include_router(aquariums_router)
    app.include_router(fish_router)
    app.include_router(feeding_router)
    app.include_router(sync_router)
    app.include_router(family_router)
    app.include_router(push_router)
    app.include_router(ai_router)
    app.include_router(gamification_router)
    app.include_router(purchase_router)
    app.include_router(admin_router)

    async_session_maker = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with async_session_maker() as session:
            yield session

    async def override_get_redis() -> AsyncGenerator[Redis]:
        yield redis_client

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis

    yield app


@pytest_asyncio.fixture(loop_scope="session")
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Create async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
