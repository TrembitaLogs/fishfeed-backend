# Інструкція з деплою

## Огляд

Проєкт використовує GitHub Actions для CI/CD з автоматичним деплоєм на Hetzner Cloud.

## Чек-ліст деплою

### 1. GitHub Secrets (в браузері)

Відкрий репозиторій на GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- [+] `HETZNER_HOST` — `91.107.235.169`
- [+] `HETZNER_USER` — `balaganov`
- [+] `HETZNER_SSH_KEY` — вміст приватного SSH ключа (весь текст з `-----BEGIN...` до `...END-----`)

### 2. SSH ключі (на локальній машині)

```bash
# Згенерувати новий ключ
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/hetzner_deploy

# Скопіювати публічний ключ на сервер
ssh-copy-id -i ~/.ssh/hetzner_deploy.pub balaganov@91.107.235.169

# Вивести приватний ключ (скопіювати в GitHub секрет HETZNER_SSH_KEY)
cat ~/.ssh/hetzner_deploy
```

- [+] Ключ згенеровано
- [+] Публічний ключ скопійовано на сервер
- [+] Приватний ключ додано в GitHub Secrets

### 3. Підготовка сервера (SSH на сервер)

```bash
ssh balaganov@91.107.235.169
```

**Встановлення Docker:**
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Перелогінитись або: newgrp docker
```

**Створення директорій:**
```bash
mkdir -p /home/balaganov/docker/fishfeed/secrets /home/balaganov/docker/fishfeed/backups
chmod 700 /home/balaganov/docker/fishfeed/secrets
```

**Авторизація в GitHub Container Registry:**
```bash
# Створи Personal Access Token на GitHub: Settings → Developer settings → Personal access tokens
# Потрібні права: read:packages
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

**Створення мережі для Traefik:**
```bash
docker network create traefik-public
```

- [+] Docker встановлено (`docker --version`)
- [+] Директорії створено (`ls -la /home/balaganov/docker/fishfeed/`)
- [+] Docker авторизовано в ghcr.io (`docker pull ghcr.io/hello-world` працює)
- [+] Мережа створена (`docker network ls | grep traefik`)

### 4. Traefik (на сервері)

Якщо Traefik ще не налаштовано, створи `/home/balaganov/docker/traefik/docker-compose.yml`:

```yaml
services:
  traefik:
    image: traefik:v3.0
    command:
      - "--api.dashboard=false"
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--entrypoints.web.http.redirections.entryPoint.to=websecure"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
      - "--certificatesresolvers.letsencrypt.acme.email=YOUR_EMAIL@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock:ro"
      - "traefik-certificates:/letsencrypt"
    networks:
      - traefik-public
    restart: unless-stopped

volumes:
  traefik-certificates:

networks:
  traefik-public:
    external: true
```

```bash
mkdir -p /home/balaganov/docker/traefik
# Створи файл вище, потім:
cd /home/balaganov/docker/traefik && docker compose up -d
```

- [+] Traefik запущено (`docker ps | grep traefik`)
- [+] Порти 80/443 відкриті (`curl -I http://91.107.235.169` → має відповісти)

### 5. Файли проєкту (з локальної машини на сервер)

```bash
# З кореня проєкту (backend/)
scp docker-compose.prod.yml balaganov@91.107.235.169:/home/balaganov/docker/fishfeed/docker-compose.yml
scp scripts/deploy.sh balaganov@91.107.235.169:/home/balaganov/docker/fishfeed/deploy.sh
scp .env.example balaganov@91.107.235.169:/home/balaganov/docker/fishfeed/.env

# Секретні файли (якщо є локально)
scp secrets/firebase-credentials.json balaganov@91.107.235.169:/home/balaganov/docker/fishfeed/secrets/
scp secrets/AuthKey.p8 balaganov@91.107.235.169:/home/balaganov/docker/fishfeed/secrets/
```

```bash
# На сервері: зробити deploy.sh виконуваним
ssh balaganov@91.107.235.169 "chmod +x /home/balaganov/docker/fishfeed/deploy.sh"
```

- [+] `docker-compose.yml` скопійовано
- [+] `deploy.sh` скопійовано та має права на виконання
- [+] `.env` скопійовано (поки з прикладу)

### 6. Конфігурація .env (на сервері)

```bash
ssh balaganov@91.107.235.169
nano /home/balaganov/docker/fishfeed/.env
```

**Обов'язково заповнити:**
```env
# Основні
POSTGRES_PASSWORD=<згенеруй: openssl rand -base64 32>
JWT_SECRET_KEY=<згенеруй: openssl rand -base64 64>
API_DOMAIN=api.yourdomain.com
GITHUB_REPOSITORY=TrembitaLogs/fishfeed-backend

# S3 Storage (Hetzner Object Storage)
S3_ENDPOINT_URL=https://fsn1.your-objectstorage.com
S3_ACCESS_KEY=<з Hetzner Console>
S3_SECRET_KEY=<з Hetzner Console>
S3_BUCKET_NAME=fishfeed-scans

# Push Notifications
FCM_PROJECT_ID=<з Firebase Console>
APNS_KEY_ID=<з Apple Developer>
APNS_TEAM_ID=<з Apple Developer>
APNS_BUNDLE_ID=com.yourcompany.fishfeed
```

- [+] `POSTGRES_PASSWORD` — випадковий пароль
- [+] `JWT_SECRET_KEY` — випадковий секрет
- [+] `API_DOMAIN` — твій домен
- [+] `GITHUB_REPOSITORY` — `TrembitaLogs/fishfeed-backend`
- [ ] S3 credentials — з Hetzner Console
- [ ] Push notification IDs — з Firebase/Apple

### 7. Secrets файли (на сервері)

Файли потрібно отримати з відповідних консолей та завантажити на сервер:

| Файл | Звідки взяти |
|------|--------------|
| `firebase-credentials.json` | [Firebase Console](https://console.firebase.google.com) → Project Settings → Service Accounts → Generate new private key |
| `AuthKey.p8` | [Apple Developer](https://developer.apple.com/account/resources/authkeys/list) → Keys → Create Key → Apple Push Notifications service (APNs) |
| `gcp-credentials.json` | [GCP Console](https://console.cloud.google.com/iam-admin/serviceaccounts) → Create Service Account → Create Key (JSON) |

```bash
# Перевірити що файли на місці
ls -la /home/balaganov/docker/fishfeed/secrets/
```

- [ ] `firebase-credentials.json` завантажено
- [ ] `AuthKey.p8` завантажено
- [ ] (опц.) `gcp-credentials.json` завантажено

### 8. DNS

У твого DNS провайдера (Cloudflare, Namecheap, тощо):

- [+] A-запис: `api.yourdomain.com` → `91.107.235.169`
- [+] Перевірка: `ping api.yourdomain.com` повертає IP сервера

### 9. Перший деплой

**Варіант А — через GitHub Actions:**
```bash
# Запуш будь-що в main
git push origin main
# Відслідковуй в GitHub → Actions
```

**Варіант Б — вручну на сервері:**
```bash
ssh balaganov@91.107.235.169
cd /home/balaganov/docker/fishfeed
./deploy.sh
```

- [ ] Деплой завершився без помилок

### 10. Верифікація

```bash
# На сервері
cd /home/balaganov/docker/fishfeed

# Статус контейнерів (всі мають бути Up)
docker compose ps

# Health check
curl -s https://api.yourdomain.com/health | jq

# Логи API (без критичних помилок)
docker compose logs --tail=50 api

# Перевірка SSL
curl -vI https://api.yourdomain.com 2>&1 | grep "SSL certificate"
```

- [ ] Всі контейнери `Up` та `healthy`
- [ ] `/health` повертає `{"status": "healthy"}`
- [ ] SSL сертифікат валідний
- [ ] В логах немає критичних помилок

---

## Архітектура Production

```
                    ┌─────────────────────────────────────────────────────────┐
                    │  traefik-public (external network)                      │
Internet ──────────▶│  ┌─────────────────────────────────────────────────┐    │
   HTTPS            │  │  Traefik (reverse proxy + SSL)                  │    │
                    │  │  - Let's Encrypt certificates                   │    │
                    │  │  - Routes: api.domain.com → fishfeed-api:8000   │    │
                    │  └─────────────────────────────────────────────────┘    │
                    │                         │                               │
                    │  ┌─────────────────────────────────────────────────┐    │
                    │  │  api (ghcr.io/trembitalogs/fishfeed-backend)    │    │
                    │  │  - FastAPI application                          │    │
                    │  │  - Healthcheck: /health                         │    │
                    │  └─────────────────────────────────────────────────┘    │
                    └─────────────────────────────────────────────────────────┘
                                              │
                    ┌─────────────────────────────────────────────────────────┐
                    │  fishfeed-internal (isolated network)                   │
                    │  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
                    │  │ postgres │  │  redis   │  │  worker  │              │
                    │  │ :17-alp  │  │ :7.4-alp │  │ feeding  │              │
                    │  │ +volume  │  │ appendonly│  │ scheduler│              │
                    │  └──────────┘  └──────────┘  └──────────┘              │
                    └─────────────────────────────────────────────────────────┘
```

**Ключові особливості:**
- **Traefik** як reverse proxy з автоматичним SSL через Let's Encrypt
- **Ізольована мережа** для бази даних та Redis (недоступні ззовні)
- **Persistence** для PostgreSQL та Redis через Docker volumes
- **Worker** для фонових задач (scheduled feeding events, notifications)

## Передумови

### Секрети GitHub репозиторію

Налаштуйте наступні секрети в налаштуваннях GitHub репозиторію (`Settings > Secrets and variables > Actions`):

| Секрет | Опис |
|--------|------|
| `HETZNER_HOST` | IP-адреса або hostname сервера |
| `HETZNER_USER` | SSH username (напр., `deploy` або `root`) |
| `HETZNER_SSH_KEY` | Приватний SSH ключ для автентифікації |

**Примітка:** `GITHUB_TOKEN` надається автоматично GitHub Actions для автентифікації в ghcr.io.

### Налаштування сервера (Hetzner)

1. **Рекомендований інстанс:** CPX31 (4 vCPU, 8GB RAM)

2. **Встановлення Docker та Docker Compose:**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```

3. **Створення директорії для деплою:**
   ```bash
   mkdir -p /home/balaganov/docker/fishfeed
   ```

4. **Автентифікація в GitHub Container Registry:**
   ```bash
   echo $GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
   ```

5. **Копіювання production файлів на сервер:**
   - `docker-compose.prod.yml` → `/home/balaganov/docker/fishfeed/docker-compose.yml`
   - `deploy.sh` → `/home/balaganov/docker/fishfeed/deploy.sh`
   - `.env.prod` → `/home/balaganov/docker/fishfeed/.env`
   - `secrets/` → `/home/balaganov/docker/fishfeed/secrets/` (Firebase, APNs credentials)

6. **Створення директорії для secrets:**
   ```bash
   mkdir -p /home/balaganov/docker/fishfeed/secrets
   chmod 700 /home/balaganov/docker/fishfeed/secrets
   ```

   Необхідні файли в `/home/balaganov/docker/fishfeed/secrets/`:
   - `firebase-credentials.json` - Firebase service account для FCM (Android push)
   - `AuthKey.p8` - Apple APNs ключ (iOS push)
   - `gcp-credentials.json` - Google Cloud credentials (якщо використовується Google Vision AI)

7. **Створення директорії для backups:**
   ```bash
   mkdir -p /home/balaganov/docker/fishfeed/backups
   ```

8. **Надання прав на виконання deploy скрипту:**
   ```bash
   chmod +x /home/balaganov/docker/fishfeed/deploy.sh
   ```

9. **Налаштування Traefik (якщо ще не налаштовано):**

   Production використовує Traefik як reverse proxy. Переконайтесь що:
   - Traefik запущено на сервері
   - Існує зовнішня мережа `traefik-public`:
     ```bash
     docker network create traefik-public
     ```
   - Налаштовано Let's Encrypt certresolver з ім'ям `letsencrypt`

### Налаштування SSH ключів

1. **Генерація пари SSH ключів (на локальній машині):**
   ```bash
   ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/hetzner_deploy
   ```

2. **Копіювання публічного ключа на сервер:**
   ```bash
   ssh-copy-id -i ~/.ssh/hetzner_deploy.pub balaganov@91.107.235.169
   ```

3. **Додавання приватного ключа до GitHub Secrets:**
   ```bash
   cat ~/.ssh/hetzner_deploy
   ```
   Скопіюйте вивід і додайте як секрет `HETZNER_SSH_KEY`.

## Процес деплою

```
Push в main → CI (lint, test) → Build Docker image → Push в ghcr.io → SSH на Hetzner → Виконання deploy.sh
```

### Автоматичний деплой

Деплой відбувається автоматично коли:
1. Код запушено в гілку `main`
2. CI workflow успішно пройшов

### Ручний деплой

Для ручного деплою на сервері:
```bash
cd /home/balaganov/docker/fishfeed
./deploy.sh
```

## Docker образи

Образи зберігаються в GitHub Container Registry:
- `ghcr.io/trembitalogs/fishfeed-backend:latest` - останній стабільний
- `ghcr.io/trembitalogs/fishfeed-backend:<sha>` - конкретний коміт

## Відкат (Rollback)

Для відкату до попередньої версії:
```bash
cd /home/balaganov/docker/fishfeed
docker compose pull ghcr.io/trembitalogs/fishfeed-backend:<previous-sha>
docker compose up -d
```

## Змінні середовища

Необхідні змінні середовища на сервері (у `/home/balaganov/docker/fishfeed/.env`):

### Обов'язкові

| Змінна | Опис | Приклад |
|--------|------|---------|
| `POSTGRES_PASSWORD` | Пароль PostgreSQL | `secure-password-here` |
| `JWT_SECRET_KEY` | Секретний ключ для JWT токенів | `your-256-bit-secret` |
| `API_DOMAIN` | Домен для API (Traefik routing) | `api.fishfeed.app` |
| `GITHUB_REPOSITORY` | GitHub репозиторій для image | `TrembitaLogs/fishfeed-backend` |

### S3 Storage (Hetzner Object Storage)

| Змінна | Опис |
|--------|------|
| `S3_ENDPOINT_URL` | URL endpoint Hetzner Object Storage |
| `S3_ACCESS_KEY` | Access key |
| `S3_SECRET_KEY` | Secret key |
| `S3_BUCKET_NAME` | Назва bucket (default: `fishfeed-scans`) |

### Push Notifications

| Змінна | Опис |
|--------|------|
| `FCM_PROJECT_ID` | Firebase project ID |
| `APNS_KEY_ID` | Apple APNs Key ID |
| `APNS_TEAM_ID` | Apple Team ID |
| `APNS_BUNDLE_ID` | iOS app bundle identifier |

### Опціональні

| Змінна | Опис |
|--------|------|
| `IMAGE_TAG` | Docker image tag (default: `latest`) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `APPLE_CLIENT_ID` | Apple OAuth client ID |
| `REVENUECAT_API_KEY` | RevenueCat API key |
| `REVENUECAT_WEBHOOK_SECRET` | RevenueCat webhook secret |
| `AI_PROVIDER` | AI provider: `google_vision` або `replicate` |

## Secrets Management

Чутливі файли зберігаються в `/home/balaganov/docker/fishfeed/secrets/` на сервері:

| Файл | Призначення | Як отримати |
|------|-------------|-------------|
| `firebase-credentials.json` | FCM (Android push) | Firebase Console → Project Settings → Service Accounts |
| `AuthKey.p8` | APNs (iOS push) | Apple Developer → Keys → Create APNs Key |
| `gcp-credentials.json` | Google Vision AI | GCP Console → IAM → Service Accounts |

**Важливо:**
- Ніколи не комітьте ці файли в git
- Використовуйте `chmod 600` для обмеження доступу
- Зберігайте резервні копії в безпечному місці

## Вирішення проблем

### Перевірка логів деплою
```bash
# На сервері
docker compose logs -f api
```

### Перевірка логів GitHub Actions
Перейдіть на вкладку `Actions` в GitHub репозиторії.

### Проблеми з SSH з'єднанням
```bash
# Тестування SSH з'єднання
ssh -i ~/.ssh/hetzner_deploy balaganov@91.107.235.169
```

### Контейнер не запускається
```bash
# Перевірка статусу контейнерів
docker compose ps

# Перевірка логів
docker compose logs api
```

### Traefik не бачить сервіс
```bash
# Перевірка що api підключено до traefik-public
docker network inspect traefik-public

# Перевірка labels контейнера
docker inspect fishfeed-api-1 | grep -A 20 Labels

# Перевірка Traefik dashboard (якщо увімкнено)
curl http://localhost:8080/api/http/services
```

### SSL сертифікат не видається
```bash
# Перевірка логів Traefik
docker logs traefik

# Переконайтесь що:
# 1. Домен вказує на IP сервера
# 2. Порти 80 та 443 відкриті
# 3. certresolver називається "letsencrypt"
```

### База даних недоступна
```bash
# Перевірка що postgres healthy
docker compose ps postgres

# Перевірка логів postgres
docker compose logs postgres

# Тест підключення з api контейнера
docker compose exec api python -c "from app.database import engine; print('OK')"
```
