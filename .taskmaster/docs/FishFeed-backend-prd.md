# FishFeed Backend — PRD

**Технології:** Python (FastAPI), PostgreSQL, Redis, AI (Vision + Species DB)  
**Deployment:** Hetzner Cloud (MVP), Docker  
**Архітектура:** REST API, Microservices-ready monolith

---

## Overview

Backend для FishFeed — серверна частина, що забезпечує синхронізацію даних, AI-розпізнавання риб, управління користувачами, Family Mode, та інтеграцію з платіжними системами.

**Проблема, яку вирішує:**
- Синхронізація даних між пристроями та членами сім'ї
- Централізована база видів риб з рекомендаціями годування
- AI-розпізнавання видів риб по фото
- Верифікація покупок та управління підписками
- Аналітика та метрики продукту

**Цільова аудиторія (споживачі API):**
- iOS та Android мобільні додатки (Flutter)
- Внутрішні сервіси (analytics, admin)
- Потенційно: web dashboard (v2)

**Цінність:**
- Надійна синхронізація для offline-first клієнтів
- Масштабована AI-інфраструктура
- GDPR-compliant зберігання даних
- Підготовка до майбутніх соціальних функцій (leaderboard, challenges)

---

## Core Features

### 1. Authentication & User Management
**Що робить:** Власна система автентифікації з JWT токенами та підтримкою OAuth провайдерів.

**Чому важливо:** Безпечна авторизація, повний контроль над user identity, незалежність від third-party сервісів.

**Як працює:**
- Реєстрація через email/password або OAuth (Google, Apple)
- Генерація JWT access token (короткий термін) + refresh token (довгий термін)
- Password hashing через bcrypt/argon2
- Зберігання refresh tokens у Redis з можливістю відкликання
- Створення/оновлення user record у PostgreSQL
- Зберігання subscription status, free_ai_scans, settings
- Soft delete для GDPR compliance

### 2. Species Database
**Що робить:** Надає curated базу видів акваріумних риб з рекомендаціями годування.

**Чому важливо:** Основа для автоматичної генерації розкладів, джерело правди для AI.

**Як працює:**
- Read-only API для клієнтів
- ~500-1000 популярних видів прісноводних риб
- Для кожного виду: назва, фото, food_type, feeding_frequency, portion_hint
- Версіонування для cache invalidation на клієнтах
- Admin API для оновлення бази

### 3. AI Fish Recognition
**Що робить:** Приймає фото, повертає визначений вид риби з confidence score.

**Чому важливо:** Wow-фактор продукту, спрощує onboarding.

**Як працює:**
- Endpoint приймає base64 image або multipart upload
- Preprocessing: resize, normalize
- Inference через ML model (hosted або cloud AI service)
- Повертає: species_id, confidence, alternatives
- Rate limiting: 5 free scans per user, unlimited для Premium
- Logging для покращення моделі

### 4. Data Sync
**Що робить:** Синхронізує дані між пристроями та членами сім'ї.

**Чому важливо:** Offline-first архітектура вимагає надійної синхронізації.

**Як працює:**
- Client надсилає локальні зміни з timestamps
- Server застосовує last-write-wins для конфліктів
- Server повертає canonical state
- Підтримка часткової синхронізації (delta sync)
- Conflict detection для критичних даних (feeding events)

### 5. Family Mode
**Що робить:** Управління спільним доступом до акваріуму.

**Чому важливо:** Вирішує біль координації в сім'ях.

**Як працює:**
- Генерація invite link з унікальним кодом
- Accept invite → додавання user до aquarium.members
- Permissions: owner може видаляти members
- Free: max 2 members, Premium: 5+
- Real-time sync feeding events між members

### 6. Push Notifications (Server-side)
**Що робить:** Тригери для push-нотифікацій на основі серверної логіки.

**Чому важливо:** Retention-критичні нотифікації, Family notifications.

**Як працює:**
- Scheduled job для "Тижневий підсумок"
- Trigger при Family mode: "Інший користувач погодував"
- Re-engagement: "Поверніться без реклами" при неактивності
- FCM integration через HTTP v1 API (для Android)
- APNs integration через python-apns2 (для iOS)
- Throttling та opt-out management

### 7. Purchase Verification
**Що робить:** Верифікує покупки з App Store/Play Store через RevenueCat webhook.

**Чому важливо:** Захист від fraud, source of truth для subscription status.

**Як працює:**
- RevenueCat webhook endpoint
- Оновлення user.subscription_status
- Оновлення user.free_ai_scans при premium
- Logging всіх транзакцій
- Graceful handling subscription expiry

### 8. Analytics Backend
**Що робить:** Приймає та зберігає events для product analytics.

**Чому важливо:** Основа для data-driven рішень.

**Як працює:**
- Events endpoint (batch support)
- Зберігання в PostgreSQL (MVP) або ClickHouse (scale)
- Інтеграція з PostHog або Amplitude (optional)
- Custom events storage для advanced analysis
- GDPR: anonymization, retention policies

---

## User Experience

### API Consumers

**Consumer 1: Mobile App (Primary)**
- Потребує: низька latency, offline support, reliable sync
- Constraints: battery, network variability
- Expectations: < 500ms response time, graceful degradation

**Consumer 2: Admin Dashboard (Future)**
- Потребує: species management, user lookup, analytics
- Constraints: internal only
- Expectations: authenticated access, audit logging

**Consumer 3: ML Pipeline (Internal)**
- Потребує: training data, model deployment
- Constraints: batch processing acceptable
- Expectations: data export, model versioning

### Key API Flows

**Flow 1: User Registration**
```
Client: POST /auth/register {email, password} або /auth/oauth {provider, token}
Server: Validate → Hash password → Create user → Generate JWT tokens
Server: Return {access_token, refresh_token, user}
```

**Flow 2: Data Sync**
```
Client: POST /sync {changes: [...], last_sync_at}
Server: Apply changes → Resolve conflicts → Return {server_state, conflicts}
Client: Apply server state → Mark synced
```

**Flow 3: AI Scan**
```
Client: POST /ai/scan {image: base64}
Server: Check scans_remaining → Process image → Return {species_id, confidence}
Server: Decrement free_scans if free user
```

**Flow 4: Family Invite**
```
Owner: POST /family/invite {aquarium_id}
Server: Generate invite_code → Return {invite_link}
Member: POST /family/accept {invite_code}
Server: Add to aquarium.members → Notify owner → Return {aquarium}
```

### Error Handling

- Standard HTTP status codes
- Consistent error response format: `{error: {code, message, details}}`
- Client-friendly error messages
- Internal error logging з context
- Rate limit responses з retry-after header

---

## Technical Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                      Load Balancer                          │
└─────────────────────────────┬───────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│                      API Gateway                            │
│              (Auth, Rate Limiting, Logging)                 │
└─────────────────────────────┬───────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
┌───────▼───────┐   ┌─────────▼─────────┐   ┌──────▼──────┐
│   Auth API    │   │    Core API       │   │   AI API    │
│               │   │                   │   │             │
│ - /auth/*     │   │ - /users/*        │   │ - /ai/scan  │
│               │   │ - /aquariums/*    │   │             │
│               │   │ - /species/*      │   │             │
│               │   │ - /sync           │   │             │
│               │   │ - /family/*       │   │             │
└───────┬───────┘   └─────────┬─────────┘   └──────┬──────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│                    PostgreSQL Database                      │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│                    Redis (Cache + Queue)                    │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│                    Background Workers                       │
│         (Notifications, Analytics, AI Processing)           │
└─────────────────────────────────────────────────────────────┘
```

### Technology Stack

**Runtime & Package Management:**
- **Python:** 3.13.x (latest stable)
- **Package Manager:** uv (fast Python package installer and resolver)
- **Virtual Environment:** managed by uv

**Framework:** FastAPI 0.115.x (latest)
- Async support для high concurrency
- Automatic OpenAPI documentation
- Pydantic v2 для validation
- Dependency injection

**Database:** PostgreSQL 17.x (latest)
- JSONB для flexible data
- Full-text search для species
- Row-level security для multi-tenancy
- Runs in separate Docker container

**Cache & Queue:** Redis 7.4.x (latest)
- Session cache
- Rate limiting counters
- Background job queue (with ARQ або Celery)
- Runs in separate Docker container

**AI/ML:**
- Option A: Self-hosted model (PyTorch/TensorFlow) + GPU instance
- Option B: Cloud AI (Google Vision API, AWS Rekognition)
- Option C: Fine-tuned model via Replicate/Hugging Face

**Infrastructure (Hetzner Cloud MVP):**
- All services run in Docker containers
- Docker Compose для orchestration
- Hetzner Cloud VPS (CPX31: 4 vCPU, 8GB RAM)
- Nginx як reverse proxy + SSL (Let's Encrypt)
- Hetzner Object Storage для images (S3-compatible)
- Можливість масштабування на Kubernetes (k3s) при зростанні

### Development Environment

**Local Setup (macOS):**
- Development machine: MacBook
- Docker Desktop for Mac
- All services run in Docker containers (identical to production)
- Hot-reload для FastAPI через volume mounts

**Docker Architecture:**
```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Compose                           │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   api       │  │  postgres   │  │   redis     │         │
│  │  (FastAPI)  │  │   (17.x)    │  │   (7.4.x)   │         │
│  │  Port 8000  │  │  Port 5432  │  │  Port 6379  │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│         │                │                │                 │
│         └────────────────┴────────────────┘                 │
│                    Docker Network                           │
└─────────────────────────────────────────────────────────────┘
```

**Docker Containers:**
| Container | Image | Purpose |
|-----------|-------|---------|
| api | python:3.13-slim + uv | FastAPI application |
| postgres | postgres:17-alpine | Database |
| redis | redis:7.4-alpine | Cache & Queue |
| worker | python:3.13-slim + uv | Background jobs (optional) |
| nginx | nginx:alpine | Reverse proxy (production) |

**CI/CD Pipeline (GitHub Actions):**
```
Push to main/PR → Lint & Type Check → Run Tests → 
→ Build Docker Image → Push to Registry → Deploy to Hetzner
```

### Data Models

```sql
-- Users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255), -- NULL for OAuth users
    oauth_provider VARCHAR(20), -- google, apple, NULL for email
    oauth_id VARCHAR(255),
    nickname VARCHAR(50),
    avatar_url TEXT,
    email_verified BOOLEAN DEFAULT FALSE,
    subscription_status VARCHAR(20) DEFAULT 'free', -- free, premium, expired
    subscription_expires_at TIMESTAMPTZ,
    free_ai_scans_remaining INT DEFAULT 5,
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ -- soft delete
);

-- Refresh Tokens
CREATE TABLE refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(255) NOT NULL,
    device_info JSONB,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_refresh_tokens_user ON refresh_tokens(user_id) WHERE revoked_at IS NULL;

-- Aquariums
CREATE TABLE aquariums (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID REFERENCES users(id),
    name VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- Aquarium Members (Family Mode)
CREATE TABLE aquarium_members (
    aquarium_id UUID REFERENCES aquariums(id),
    user_id UUID REFERENCES users(id),
    role VARCHAR(20) DEFAULT 'member', -- owner, member
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (aquarium_id, user_id)
);

-- Family Invites
CREATE TABLE family_invites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aquarium_id UUID REFERENCES aquariums(id),
    invite_code VARCHAR(32) UNIQUE NOT NULL,
    created_by UUID REFERENCES users(id),
    expires_at TIMESTAMPTZ NOT NULL,
    used_by UUID REFERENCES users(id),
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Species Database
CREATE TABLE species (
    id VARCHAR(50) PRIMARY KEY, -- slug: "betta-splendens"
    common_name VARCHAR(100) NOT NULL,
    scientific_name VARCHAR(100),
    image_url TEXT,
    food_types JSONB NOT NULL, -- ["flakes", "pellets"]
    feeding_frequency INT NOT NULL, -- times per day
    portion_hint TEXT,
    care_level VARCHAR(20), -- beginner, intermediate, advanced
    water_type VARCHAR(20) DEFAULT 'freshwater',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Fish (User's fish instances)
CREATE TABLE fish (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aquarium_id UUID REFERENCES aquariums(id),
    species_id VARCHAR(50) REFERENCES species(id),
    quantity INT DEFAULT 1,
    custom_name VARCHAR(50),
    added_via VARCHAR(20) DEFAULT 'manual', -- manual, ai_scan
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- Feeding Schedules
CREATE TABLE feeding_schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aquarium_id UUID REFERENCES aquariums(id),
    times_per_day INT NOT NULL,
    scheduled_times JSONB NOT NULL, -- ["08:00", "20:00"]
    food_type VARCHAR(50),
    portion_hint TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Feeding Events
CREATE TABLE feeding_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aquarium_id UUID REFERENCES aquariums(id),
    schedule_id UUID REFERENCES feeding_schedules(id),
    scheduled_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) DEFAULT 'pending', -- pending, fed, missed
    completed_at TIMESTAMPTZ,
    completed_by UUID REFERENCES users(id),
    client_created_at TIMESTAMPTZ, -- for sync
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Streaks
CREATE TABLE streaks (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    current_streak INT DEFAULT 0,
    best_streak INT DEFAULT 0,
    freeze_available INT DEFAULT 2,
    freeze_used_this_period BOOLEAN DEFAULT FALSE,
    period_start DATE,
    last_feed_date DATE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Achievements
CREATE TABLE achievements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    achievement_type VARCHAR(50) NOT NULL,
    unlocked_at TIMESTAMPTZ DEFAULT NOW(),
    shared_at TIMESTAMPTZ,
    UNIQUE(user_id, achievement_type)
);

-- AI Scans Log
CREATE TABLE ai_scans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    image_hash VARCHAR(64), -- for dedup
    detected_species_id VARCHAR(50),
    confidence FLOAT,
    alternatives JSONB,
    confirmed_species_id VARCHAR(50),
    was_corrected BOOLEAN DEFAULT FALSE,
    processing_time_ms INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Push Tokens
CREATE TABLE push_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    token TEXT NOT NULL,
    platform VARCHAR(20) NOT NULL, -- ios, android
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, token)
);

-- Notification Preferences
CREATE TABLE notification_preferences (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    feeding_reminders BOOLEAN DEFAULT TRUE,
    overdue_alerts BOOLEAN DEFAULT TRUE,
    streak_protection BOOLEAN DEFAULT TRUE,
    weekly_summary BOOLEAN DEFAULT TRUE,
    family_updates BOOLEAN DEFAULT TRUE,
    marketing BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_feeding_events_aquarium_scheduled 
    ON feeding_events(aquarium_id, scheduled_at);
CREATE INDEX idx_feeding_events_status 
    ON feeding_events(status) WHERE status = 'pending';
CREATE INDEX idx_fish_aquarium 
    ON fish(aquarium_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_species_search 
    ON species USING gin(to_tsvector('english', common_name || ' ' || scientific_name));
```

### API Endpoints

**Authentication**
```
POST /auth/register          - Register with email/password
POST /auth/login             - Login, get tokens
POST /auth/oauth             - OAuth login (Google, Apple)
POST /auth/refresh           - Refresh access token
POST /auth/logout            - Revoke refresh token
POST /auth/password/reset    - Request password reset
POST /auth/password/change   - Change password
DELETE /auth/account         - Delete account (GDPR)
```

**Users**
```
GET /users/me                - Get current user profile
PUT /users/me                - Update profile
GET /users/me/stats          - Get statistics (streak, achievements)
PUT /users/me/settings       - Update settings
PUT /users/me/notifications  - Update notification preferences
```

**Species**
```
GET /species                 - List all species (paginated, cached)
GET /species/{id}            - Get species details
GET /species/search?q=       - Search species
GET /species/popular         - Popular species for onboarding
```

**Aquariums**
```
GET /aquariums               - List user's aquariums
POST /aquariums              - Create aquarium
GET /aquariums/{id}          - Get aquarium with fish
PUT /aquariums/{id}          - Update aquarium
DELETE /aquariums/{id}       - Delete aquarium
```

**Fish**
```
POST /aquariums/{id}/fish    - Add fish to aquarium
PUT /fish/{id}               - Update fish
DELETE /fish/{id}            - Remove fish
```

**Schedules**
```
GET /aquariums/{id}/schedule     - Get feeding schedule
PUT /aquariums/{id}/schedule     - Update schedule
POST /aquariums/{id}/schedule/generate  - Auto-generate based on fish
```

**Feeding Events**
```
GET /aquariums/{id}/events           - Get events (date range)
GET /aquariums/{id}/events/today     - Today's events
POST /aquariums/{id}/events/{id}/fed - Mark as fed
POST /aquariums/{id}/events/{id}/missed - Mark as missed
```

**Sync**
```
POST /sync                   - Sync local changes
```

**Family**
```
GET /aquariums/{id}/family           - Get family members
POST /aquariums/{id}/family/invite   - Create invite
POST /family/accept                  - Accept invite
DELETE /aquariums/{id}/family/{user_id} - Remove member
```

**AI**
```
POST /ai/scan                - Scan image for fish species
GET /ai/scans/remaining      - Get remaining free scans
```

**Purchases**
```
POST /purchases/webhook      - RevenueCat webhook
POST /purchases/restore      - Restore purchases
```

**Push Tokens**
```
POST /push/token             - Register push token
DELETE /push/token           - Unregister token
```

### Infrastructure Requirements

**Development Environment (macOS):**
- MacBook з Docker Desktop
- Docker Compose для локальної розробки
- Всі сервіси ідентичні production

**MVP Setup (Hetzner Cloud):**
- 1x CPX31 (4 vCPU, 8GB RAM, 160GB SSD) — all containers
- Docker Compose або Docker Swarm
- Hetzner Object Storage — для AI scan images
- Estimated cost: ~€20-30/month

**Docker Containers (Production):**
```yaml
services:
  api:
    image: ghcr.io/your-org/fishfeed-api:latest
    ports: ["8000:8000"]
    depends_on: [postgres, redis]
    
  postgres:
    image: postgres:17-alpine
    volumes: [postgres_data:/var/lib/postgresql/data]
    
  redis:
    image: redis:7.4-alpine
    volumes: [redis_data:/data]
    
  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    
  worker:
    image: ghcr.io/your-org/fishfeed-api:latest
    command: ["python", "-m", "app.worker"]
```

**Scale Setup (при зростанні):**
- 2x CPX31 за Hetzner Load Balancer — API containers
- 1x CPX21 — Worker containers
- 1x CPX41 — PostgreSQL container з volume backup
- Dedicated Redis container
- Optional: GPU instance для AI (CCX або зовнішній)

**Database:**
- PostgreSQL 17.x in Docker container
- Persistent volume for data
- Automated backups to Object Storage

**Cache:**
- Redis 7.4.x in Docker container
- Persistent volume (optional, for queue)

**Storage:**
- Hetzner Object Storage (S3-compatible)
- ~1MB per scan, retention 30 days

**Networking:**
- HTTPS only (Nginx container + Let's Encrypt via certbot)
- Docker network для internal communication
- Hetzner Private Network (optional)
- Optional: Cloudflare CDN для species images

**CI/CD (GitHub Actions):**
```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Push/PR   │───▶│  Lint/Test  │───▶│ Build Image │
└─────────────┘    └─────────────┘    └──────┬──────┘
                                             │
┌─────────────┐    ┌─────────────┐           │
│   Deploy    │◀───│  Push GHCR  │◀──────────┘
│  (Hetzner)  │    │             │
└─────────────┘    └─────────────┘
```

**Monitoring:**
- Application: Sentry (free tier)
- Metrics: Prometheus + Grafana (in Docker)
- Logs: Docker logs → Loki або файли
- Uptime: UptimeRobot або Healthchecks.io

---

## Development Roadmap

### Phase 1: Foundation

**Scope:**

*Project Setup:*
- Initialize project with uv (`uv init`, `uv add`)
- FastAPI project structure (routers, services, models, schemas)
- Dockerfile for API container (Python 3.13 + uv)
- Docker Compose with api, postgres, redis containers
- .env files for local/production configs
- Makefile for common commands

*Development Environment (macOS):*
- Docker Desktop installation
- docker-compose up для запуску всіх сервісів
- Volume mounts для hot-reload
- VS Code Dev Containers support (optional)

*Database & Auth:*
- PostgreSQL 17 container setup
- Redis 7.4 container setup
- Database migrations (Alembic)
- PostgreSQL schema (users, refresh_tokens)
- Native JWT authentication (register, login, refresh, logout)
- OAuth integration (Google, Apple)
- Password hashing (argon2)

*CI/CD (GitHub Actions):*
- Lint: ruff
- Type check: mypy
- Tests: pytest
- Build Docker image
- Push to GitHub Container Registry (ghcr.io)
- Deploy to Hetzner via SSH

*Basic Features:*
- Health check endpoint
- OpenAPI documentation (auto-generated)
- Basic CRUD for users
- Structured logging

**Deliverables:**
- Working API with native auth
- Database migrations setup (Alembic)
- Docker Compose for local dev (api + postgres + redis)
- GitHub Actions pipeline (lint → test → build → deploy)
- Deployment script for Hetzner

### Phase 2: Species Database

**Scope:**
- Species table та seed data
- CRUD API for species
- Search functionality
- Popular species endpoint
- Caching strategy (Redis)
- Admin API для management

**Deliverables:**
- Species API ready
- ~200 species seeded

### Phase 3: Core Data Models

**Scope:**
- Aquariums CRUD
- Fish CRUD
- Feeding schedules
- Schedule auto-generation logic
- Feeding events creation

**Deliverables:**
- Full data model working
- Auto-schedule generation

### Phase 4: Sync Engine

**Scope:**
- Sync endpoint implementation
- Conflict resolution logic
- Delta sync support
- Sync logging та debugging
- Performance optimization

**Deliverables:**
- Reliable sync working
- Conflict handling tested

### Phase 5: Family Mode

**Scope:**
- Aquarium members management
- Invite generation та acceptance
- Permissions enforcement
- Free vs Premium limits
- Family notifications trigger

**Deliverables:**
- Family sharing working
- Limits enforced

### Phase 6: AI Integration

**Scope:**
- AI scan endpoint
- Image preprocessing
- ML model integration (cloud або self-hosted)
- Confidence thresholds
- Free scans tracking
- Scan logging для model improvement

**Deliverables:**
- AI recognition working
- Limits enforced

### Phase 7: Push Notifications

**Scope:**
- Push token management
- FCM HTTP v1 API integration (Android)
- APNs integration via python-apns2 (iOS)
- Server-triggered notifications
- Notification preferences
- Scheduled notifications (weekly summary) via APScheduler/Celery Beat
- Rate limiting та throttling

**Deliverables:**
- Server-side push working
- Preferences respected

### Phase 8: Gamification Backend

**Scope:**
- Streak calculation logic
- Freeze day handling
- Achievement definitions
- Achievement unlock triggers
- Stats aggregation

**Deliverables:**
- Gamification data accurate
- Achievements triggering

### Phase 9: Monetization & Analytics

**Scope:**
- RevenueCat webhook
- Subscription status management
- Premium feature gates
- Analytics events storage
- GDPR compliance checks
- Rate limiting refinement

**Deliverables:**
- Purchases verified
- Analytics stored

### Phase 10: Production Readiness

**Scope:**
- Load testing
- Security audit
- Performance optimization
- Monitoring та alerting
- Documentation
- Runbooks

**Deliverables:**
- Production-ready system
- Ops documentation

### Future Enhancements (Post-MVP)

**V2 Features:**
- Leaderboard API
- Challenges system
- Vacation mode
- Advanced analytics dashboard
- Multi-region deployment
- Real-time sync (WebSockets)
- ML model retraining pipeline

---

## Logical Dependency Chain

```
Phase 1: Foundation
    │
    ├── Auth + Basic DB + CI/CD
    │
    ▼
Phase 2: Species Database
    │
    ├── Requires: Phase 1
    ├── Enables: Fish adding, Schedule generation
    │
    ▼
Phase 3: Core Data Models ← ПЕРШИЙ USABLE BACKEND
    │
    ├── Requires: Phase 1, 2
    ├── Enables: Mobile app core loop
    │
    ▼
Phase 4: Sync Engine
    │
    ├── Requires: Phase 3
    ├── Enables: Multi-device, offline support
    │
    ▼
Phase 5: Family Mode
    │
    ├── Requires: Phase 3, 4 (sync)
    ├── Can parallel with: Phase 6
    │
    ▼
Phase 6: AI Integration
    │
    ├── Requires: Phase 2 (species)
    ├── Can parallel with: Phase 5
    │
    ▼
Phase 7: Push Notifications
    │
    ├── Requires: Phase 3 (events), Phase 5 (family)
    │
    ▼
Phase 8: Gamification Backend
    │
    ├── Requires: Phase 3 (feeding events)
    ├── Can parallel with: Phase 7
    │
    ▼
Phase 9: Monetization
    │
    ├── Requires: Phase 6 (AI limits), Phase 5 (family limits)
    │
    ▼
Phase 10: Production Readiness
    │
    └── Requires: All above
```

**Критичний шлях:** Phase 1 → Phase 2 → Phase 3 → Phase 4

**Паралельні потоки після Phase 3:**
- Stream A: Phase 5 (Family) → Phase 7 (Push)
- Stream B: Phase 6 (AI) → Phase 9 (Monetization)
- Stream C: Phase 8 (Gamification)

---

## Risks and Mitigations

### Technical Challenges

| Risk | Impact | Mitigation |
|------|--------|------------|
| Sync conflicts data loss | Critical | Comprehensive testing, conflict logging, user notification |
| AI accuracy < 80% | High | Fallback to manual, confidence thresholds, user correction |
| Database performance at scale | High | Indexing strategy, read replicas, query optimization |
| Cold start latency (serverless) | Medium | Provisioned concurrency або always-on instances |

### MVP Definition

| Risk | Impact | Mitigation |
|------|--------|------------|
| Over-engineering sync | High | Start with simple last-write-wins |
| AI infra complexity | High | Start with cloud AI service, migrate later |
| Premature optimization | Medium | Profile first, optimize bottlenecks |

### Resource Constraints

| Risk | Impact | Mitigation |
|------|--------|------------|
| Single point of failure | High | Multi-AZ deployment, health checks |
| Cost overrun (AI) | Medium | Usage monitoring, free tier limits |
| Security breach | Critical | Security audit, penetration testing, encryption |

### Compliance

| Risk | Impact | Mitigation |
|------|--------|------------|
| GDPR violation | Critical | Data retention policies, deletion API, consent tracking |
| App Store rejection | High | Privacy policy, data handling documentation |

---

## Appendix

### API Response Formats

**Success Response:**
```json
{
  "data": { ... },
  "meta": {
    "request_id": "uuid",
    "timestamp": "2024-01-01T00:00:00Z"
  }
}
```

**Error Response:**
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human readable message",
    "details": { ... }
  },
  "meta": {
    "request_id": "uuid",
    "timestamp": "2024-01-01T00:00:00Z"
  }
}
```

### Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| AUTH_INVALID_CREDENTIALS | 401 | Email or password incorrect |
| AUTH_INVALID_TOKEN | 401 | JWT token invalid or malformed |
| AUTH_EXPIRED | 401 | Token expired |
| AUTH_REVOKED | 401 | Refresh token revoked |
| FORBIDDEN | 403 | No access to resource |
| NOT_FOUND | 404 | Resource not found |
| VALIDATION_ERROR | 400 | Request validation failed |
| EMAIL_EXISTS | 409 | Email already registered |
| CONFLICT | 409 | Sync conflict |
| RATE_LIMITED | 429 | Too many requests |
| AI_SCAN_LIMIT | 402 | Free scans exhausted |
| PREMIUM_REQUIRED | 402 | Feature requires premium |
| SERVER_ERROR | 500 | Internal error |

### Environment Variables

```
# Database
DATABASE_URL=postgresql://user:pass@db-host:5432/fishfeed
REDIS_URL=redis://redis-host:6379/0

# JWT Auth
JWT_SECRET_KEY=your-secret-key-min-32-chars
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=30

# OAuth Providers
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
APPLE_CLIENT_ID=...
APPLE_TEAM_ID=...
APPLE_KEY_ID=...
APPLE_PRIVATE_KEY=...

# AI Service
AI_SERVICE_URL=...
AI_API_KEY=...

# Push Notifications
FCM_PROJECT_ID=...
FCM_SERVICE_ACCOUNT_JSON=...
APNS_KEY_ID=...
APNS_TEAM_ID=...
APNS_AUTH_KEY=...
APNS_BUNDLE_ID=...

# RevenueCat
REVENUECAT_WEBHOOK_SECRET=...

# Object Storage (S3-compatible)
S3_ENDPOINT=https://fsn1.your-objectstorage.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=fishfeed-uploads

# App
ENVIRONMENT=production
LOG_LEVEL=INFO
CORS_ORIGINS=https://app.fishfeed.com
```

### SLA Targets

| Metric | Target |
|--------|--------|
| API Availability | 99.9% |
| P50 Latency | < 100ms |
| P99 Latency | < 500ms |
| AI Scan P50 | < 2s |
| Sync Success Rate | > 99.5% |

### Security Checklist

- [ ] All endpoints require authentication (except health, species list, auth endpoints)
- [ ] JWT signature verification on every request
- [ ] Refresh token rotation on use
- [ ] Password hashing with argon2/bcrypt
- [ ] SQL injection prevention (parameterized queries via SQLAlchemy)
- [ ] Rate limiting per user та per IP
- [ ] Input validation (Pydantic)
- [ ] HTTPS only (Nginx + Let's Encrypt)
- [ ] Secrets in environment variables
- [ ] Database credentials with minimal privileges
- [ ] Audit logging for sensitive operations
- [ ] GDPR data export та deletion
- [ ] OAuth state parameter validation
- [ ] CORS properly configured

### Project Structure

```
fishfeed-backend/
├── .github/
│   └── workflows/
│       ├── ci.yml              # Lint, test on PR
│       └── deploy.yml          # Build, push, deploy on main
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Settings from env
│   ├── database.py             # SQLAlchemy setup
│   ├── dependencies.py         # Common dependencies
│   ├── api/
│   │   ├── __init__.py
│   │   ├── auth.py             # Auth endpoints
│   │   ├── users.py            # User endpoints
│   │   ├── species.py          # Species endpoints
│   │   ├── aquariums.py        # Aquarium endpoints
│   │   ├── feeding.py          # Feeding endpoints
│   │   ├── family.py           # Family endpoints
│   │   ├── ai.py               # AI scan endpoints
│   │   ├── sync.py             # Sync endpoint
│   │   └── push.py             # Push token endpoints
│   ├── models/                 # SQLAlchemy models
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── aquarium.py
│   │   ├── fish.py
│   │   ├── feeding.py
│   │   └── ...
│   ├── schemas/                # Pydantic schemas
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── user.py
│   │   └── ...
│   ├── services/               # Business logic
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── feeding.py
│   │   ├── ai.py
│   │   └── ...
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── jwt.py
│   │   ├── password.py
│   │   └── ...
│   └── worker.py               # Background worker entry
├── alembic/
│   ├── versions/               # Migration files
│   └── env.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Fixtures
│   ├── test_auth.py
│   └── ...
├── scripts/
│   ├── seed_species.py         # Seed species data
│   └── deploy.sh               # Deployment script
├── Dockerfile
├── docker-compose.yml          # Local development
├── docker-compose.prod.yml     # Production
├── pyproject.toml              # uv project config
├── uv.lock                     # uv lock file
├── alembic.ini
├── Makefile
├── .env.example
└── README.md
```

### Key Configuration Files

**pyproject.toml (uv):**
```toml
[project]
name = "fishfeed-backend"
version = "0.1.0"
description = "FishFeed Backend API"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "sqlalchemy>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",
    "pydantic>=2.10.0",
    "pydantic-settings>=2.6.0",
    "python-jose[cryptography]>=3.3.0",
    "passlib[argon2]>=1.7.4",
    "redis>=5.2.0",
    "httpx>=0.28.0",
    "python-multipart>=0.0.12",
    "sentry-sdk[fastapi]>=2.19.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=6.0.0",
    "ruff>=0.8.0",
    "mypy>=1.13.0",
    "httpx>=0.28.0",
]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.mypy]
python_version = "3.13"
strict = true
```

**Dockerfile:**
```dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Run with uvicorn
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**docker-compose.yml (Development):**
```yaml
version: "3.9"

services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - ./app:/app/app  # Hot reload
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/fishfeed
      - REDIS_URL=redis://redis:6379/0
      - JWT_SECRET_KEY=dev-secret-key-change-in-production
      - ENVIRONMENT=development
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    command: ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: fishfeed
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7.4-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

**Makefile:**
```makefile
.PHONY: dev up down logs test lint migrate seed

# Start all services
up:
	docker-compose up -d

# Stop all services
down:
	docker-compose down

# View logs
logs:
	docker-compose logs -f api

# Run tests
test:
	docker-compose exec api uv run pytest

# Run linter
lint:
	docker-compose exec api uv run ruff check .
	docker-compose exec api uv run mypy app

# Run migrations
migrate:
	docker-compose exec api uv run alembic upgrade head

# Create new migration
migration:
	docker-compose exec api uv run alembic revision --autogenerate -m "$(msg)"

# Seed species data
seed:
	docker-compose exec api uv run python scripts/seed_species.py

# Shell into api container
shell:
	docker-compose exec api bash

# Full reset (careful!)
reset:
	docker-compose down -v
	docker-compose up -d
	make migrate
	make seed
```

### Development Workflow

```bash
# Initial setup (one time)
git clone https://github.com/your-org/fishfeed-backend.git
cd fishfeed-backend
cp .env.example .env

# Start development
make up                  # Start all containers
make migrate             # Run migrations
make seed                # Seed species data

# Daily development
make logs                # Watch API logs
make test                # Run tests
make lint                # Check code style

# Database changes
make migration msg="add new field"
make migrate

# Stop development
make down
```