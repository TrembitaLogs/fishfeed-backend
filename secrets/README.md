# Secrets Directory

This directory contains sensitive files for production deployment.

## Required files:

- `firebase-credentials.json` - Firebase service account for FCM (Android push notifications)
- `AuthKey.p8` - Apple APNs authentication key (iOS push notifications)
- `gcp-credentials.json` - Google Cloud credentials (if using Google Vision AI)

## Security Notes:

- **NEVER** commit these files to git
- Keep backups in a secure location
- Rotate keys periodically

