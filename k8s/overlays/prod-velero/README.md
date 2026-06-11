# Velero — weekly Vespa backup (self-managed, prod)

Standalone Velero install for the `darwin` AKS cluster. **Deliberately separate
from the app overlay** (like `k8s/overlays/prod-vespa`): `kubectl apply -k
k8s/overlays/prod` never touches it. This tree owns Velero, its weekly backup
schedule, the failure alerts, and the Slack notifier — nothing else.

## What it backs up, and why

Only the Vespa state that can't be cheaply rebuilt — the 6 PVCs labeled
`backup=vespa` in namespace `darwin`:

- `vespa-var-vespa-content-{0,1,2}` (3×100Gi) — the search index
- `vespa-var1-vespa-configserver-{0,1,2}` (3×5Gi) — deployed app package/schema

Everything else is intentionally excluded (model caches re-download; logs and
workspace are scratch; `dynamic-pvc`/`file-connector-pvc` are unused; Redis is
cache). Postgres — the real source of truth — is external + Azure-managed and
backed up separately.

**Consistency caveat:** disk snapshots are point-in-time *per volume*, not
coordinated across the 3 content nodes. Treat this as a fast-DR accelerator;
the authoritative recovery path is re-indexing from Postgres.

## Schedule & retention

- Weekly, Sundays 02:00 UTC (`schedule.yaml`).
- `ttl: 504h` (21 days) → the **last 3 weekly backups** are retained; the
  21-day-old one is garbage-collected (snapshots GC'd too). Worst-case data
  loss on a crash is up to one week; max rollback is 21 days.

## Azure wiring (reused, no new role assignments)

- Auth: SP `155fae3c-6bce-46be-8c15-25adf84b4c4e`, which already holds
  `Contributor` on `darwin-backups`, `MC_darwin_darwin_westeurope`, and
  `darwin` — so **no Owner/UAA ticket is needed**. Only a fresh client secret.
- Backup metadata store (`BackupStorageLocation`): SA `darwinaksbackup` /
  container `darwinaksbackup` in RG `darwin-backups` (the old store, purged).
- Disk snapshots (`VolumeSnapshotLocation`): node RG
  `MC_darwin_darwin_westeurope` — NOT the lock-protected `darwin` RG, so TTL GC
  can delete them.

## Alerting (two layers)

1. **PrometheusRule + ServiceMonitor** (`release: robusta` label → picked up by
   the existing robusta Prometheus/Alertmanager). Real-time failure + staleness.
   `VeleroNoRecentSuccessfulBackup` is the safety net that watches the *outcome*
   (time since last success) — it would have caught the previous silent
   ~year-long failure regardless of cause.
2. **Notifier CronJob** (`notifier.yaml`) — Sundays 03:00 UTC, posts the latest
   run's result (✅/⚠️/❌, both success and failure) to **#darwin-devs** via the
   Slack bot token.

## One-time setup (in order)

```bash
# context must be darwin
kubectl config current-context   # -> darwin

# 1. CRDs (version plumbing, not config — bootstrap once). Either:
velero install --crds-only        # if the velero CLI is handy, OR
kubectl apply -f https://raw.githubusercontent.com/vmware-tanzu/velero/v1.13.2/config/crd/v1/crds/crds.yaml

# 2. Azure SP secret — create a FRESH client secret on app 155fae3c
#    (as an OWNER OF THE APP REGISTRATION; NOT subscription Owner/UAA), then:
cp credentials-velero.example credentials-velero
#    edit credentials-velero -> paste AZURE_CLIENT_SECRET

# 3. Slack notifier — reuse the existing bot token, and INVITE the bot to
#    #darwin-devs (the bot-token path posts by channel, not the email address):
cp slack-notify.env.example slack-notify.env
kubectl get secret danswer-secrets -n darwin \
  -o jsonpath='{.data.DANSWER_BOT_SLACK_BOT_TOKEN}' | base64 -d   # -> SLACK_BOT_TOKEN

# 4. Apply
kubectl apply -k k8s/overlays/prod-velero

# 5. Validate
kubectl -n velero get pods                        # velero Running
kubectl -n velero get backupstoragelocation       # PHASE Available (NOT Unavailable)
velero backup create --from-schedule vespa-weekly  # on-demand test run (or wait for Sunday)
kubectl -n velero get backups.velero.io            # PHASE Completed
```

`credentials-velero` and `slack-notify.env` are gitignored — never commit them.

## Restore (outline)

```bash
velero restore create --from-backup <backup-name> \
  --include-namespaces darwin --selector backup=vespa
```
Restoring recreates the PVCs from snapshots. Coordinate with the Vespa
StatefulSets (scale down content/configserver, restore, scale up) — see
k8s/scripts and the Vespa overlay.

## History

The previous self-managed Velero (same SP, same SA) silently failed for ~a year
(volume snapshots stopped 2025-06-10; BSL auth died on an expired SP secret).
Removed 2026-06-11 along with its 130 stale snapshots and 63 backup blobs. This
install is the revival — same approach, now with **failure + staleness alerts**
and a **success/failure notifier** so it can't fail silently again. Set a
long-lived SP secret (and a renewal reminder); nothing rotates it automatically.
</content>
