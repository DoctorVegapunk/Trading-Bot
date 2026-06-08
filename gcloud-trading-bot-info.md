# Google Cloud — Trading Bot Project Reference

## Project
| Field | Value |
|---|---|
| Project ID | `trading-bot-498206` |
| Project Number | `544780539794` |
| Region | `us-central1` |

---

## Cloud Run Service
| Field | Value |
|---|---|
| Service Name | `trading-bot` |
| Service URL | `https://trading-bot-544780539794.us-central1.run.app` |
| Trigger Endpoint | `https://trading-bot-544780539794.us-central1.run.app/run` |
| Health Endpoint | `https://trading-bot-544780539794.us-central1.run.app/health` |
| Region | `us-central1` |
| Memory | `1Gi` |
| Timeout | `300s` |
| Max Instances | `1` |
| Current Revision | `trading-bot-00005-8w4` |

---

## Cloud Scheduler
| Field | Value |
|---|---|
| Job Name | `trading-bot-hourly` |
| Schedule | `0 * * * *` (top of every hour) |
| Timezone | `Africa/Nairobi (EAT, UTC+3)` |
| HTTP Method | `POST` |
| Auth | OIDC |
| Invoker SA | `scheduler-invoker@trading-bot-498206.iam.gserviceaccount.com` |
| Location | `us-central1` |
| State | `ENABLED` |
| Last Run | `2026-06-07 11:23 UTC` (200 OK) |

---

## Service Accounts
| Account | Role | Purpose |
|---|---|---|
| `scheduler-invoker@trading-bot-498206.iam.gserviceaccount.com` | `roles/run.invoker` | Allows Cloud Scheduler to trigger Cloud Run |
| `544780539794-compute@developer.gserviceaccount.com` | `roles/secretmanager.secretAccessor` | Allows Cloud Run to read secrets |

---

## Secrets (Secret Manager)
| Secret Name | Purpose |
|---|---|
| `FIX_PASSWORD` | Pepperstone/cTrader FIX login password |
| `FIX_HOST` | cTrader FIX hostname |
| `FIX_QUOTE_PORT` | Market data port (5211) |
| `FIX_TRADE_PORT` | Order execution port (5212) |
| `FIX_USE_SSL` | TLS enabled |
| `FIX_SENDER_COMP_ID` | FIX sender comp ID |
| `FIX_TARGET_COMP_ID` | FIX target comp ID (cServer) |
| `FIX_ACCOUNT` | Trading account number |
| `FIX_SENDER_SUB_ID` | FIX sender sub ID |
| `FIX_BALANCE` | Account balance for position sizing |
| `FIX_CURRENCY` | Balance currency (USD) |
| `OPEN_API_CLIENT_ID` | cTrader Open API client ID |
| `OPEN_API_CLIENT_SECRET` | cTrader Open API client secret |
| `OPEN_API_CTID_TRADER_ACCOUNT_ID` | Open API account ID |
| `OPEN_API_REFRESH_TOKEN` | OAuth 2.0 refresh token for Open API (fileless auth on Cloud Run) |
| `OPEN_API_TOKEN_JSON` | Full OAuth token JSON (access + refresh tokens + expiry) — written to disk at startup |

New secrets are created via `gcloud secrets create` + IAM binding (see commands below). All secrets use the `544780539794-compute@developer.gserviceaccount.com` service account with `roles/secretmanager.secretAccessor`.

---

## Enabled APIs
- `run.googleapis.com` — Cloud Run
- `cloudscheduler.googleapis.com` — Cloud Scheduler
- `cloudbuild.googleapis.com` — Cloud Build
- `artifactregistry.googleapis.com` — Artifact Registry
- `secretmanager.googleapis.com` — Secret Manager

---

## Useful Commands

### Create the Cloud Scheduler (one-time setup)
```powershell
gcloud iam service-accounts create scheduler-invoker --display-name "Scheduler Invoker"

gcloud projects add-iam-policy-binding trading-bot-498206 `
  --member="serviceAccount:scheduler-invoker@trading-bot-498206.iam.gserviceaccount.com" `
  --role="roles/run.invoker"

gcloud scheduler jobs create http trading-bot-hourly `
  --schedule="0 * * * 1-5" `
  --uri="https://trading-bot-544780539794.us-central1.run.app/run" `
  --http-method=POST `
  --oidc-service-account-email="scheduler-invoker@trading-bot-498206.iam.gserviceaccount.com" `
  --location=us-central1 `
  --time-zone="UTC"
```

### Redeploy after code changes
```powershell
gcloud run deploy trading-bot `
  --source . `
  --region us-central1 `
  --no-allow-unauthenticated `
  --memory 1Gi `
  --timeout 300 `
  --max-instances 1 `
  --set-secrets="FIX_PASSWORD=FIX_PASSWORD:latest,FIX_HOST=FIX_HOST:latest,FIX_QUOTE_PORT=FIX_QUOTE_PORT:latest,FIX_TRADE_PORT=FIX_TRADE_PORT:latest,FIX_USE_SSL=FIX_USE_SSL:latest,FIX_SENDER_COMP_ID=FIX_SENDER_COMP_ID:latest,FIX_TARGET_COMP_ID=FIX_TARGET_COMP_ID:latest,FIX_ACCOUNT=FIX_ACCOUNT:latest,FIX_SENDER_SUB_ID=FIX_SENDER_SUB_ID:latest,FIX_BALANCE=FIX_BALANCE:latest,FIX_CURRENCY=FIX_CURRENCY:latest,OPEN_API_CLIENT_ID=OPEN_API_CLIENT_ID:latest,OPEN_API_CLIENT_SECRET=OPEN_API_CLIENT_SECRET:latest,OPEN_API_CTID_TRADER_ACCOUNT_ID=OPEN_API_CTID_TRADER_ACCOUNT_ID:latest,OPEN_API_REFRESH_TOKEN=OPEN_API_REFRESH_TOKEN:latest,OPEN_API_TOKEN_JSON=OPEN_API_TOKEN_JSON:latest"
```

### Add/update environment variables
```powershell
gcloud run services update trading-bot `
  --region us-central1 `
  --set-env-vars="VAR_NAME=value,VAR_NAME2=value2"
```

### Add a new secret
```powershell
gcloud secrets create SECRET_NAME --data-file=- <<< "SECRET_VALUE"

gcloud secrets add-iam-policy-binding SECRET_NAME `
  --member="serviceAccount:544780539794-compute@developer.gserviceaccount.com" `
  --role="roles/secretmanager.secretAccessor"
```

### Grant Cloud Run service account Firestore access
```powershell
gcloud projects add-iam-policy-binding trading-bot-498206 `
  --member="serviceAccount:544780539794-compute@developer.gserviceaccount.com" `
  --role="roles/datastore.user"
```

### Update an existing secret value
```powershell
gcloud secrets versions add SECRET_NAME --data-file=- <<< "NEW_VALUE"
```

### Trigger bot manually
```powershell
gcloud scheduler jobs run trading-bot-hourly --location=us-central1
```

### View live logs
```powershell
gcloud run services logs read trading-bot --region=us-central1 --limit=50
```

### Pause the scheduler (stop hourly runs)
```powershell
gcloud scheduler jobs pause trading-bot-hourly --location=us-central1
```

### Resume the scheduler
```powershell
gcloud scheduler jobs resume trading-bot-hourly --location=us-central1
```

---

## Console Links
- **Cloud Run:** https://console.cloud.google.com/run?project=trading-bot-498206
- **Cloud Scheduler:** https://console.cloud.google.com/cloudscheduler?project=trading-bot-498206
- **Secret Manager:** https://console.cloud.google.com/security/secret-manager?project=trading-bot-498206
- **Cloud Build Logs:** https://console.cloud.google.com/cloud-build/builds;region=us-central1?project=trading-bot-498206
- **Artifact Registry:** https://console.cloud.google.com/artifacts?project=trading-bot-498206
