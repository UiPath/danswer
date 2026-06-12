# Azure-managed AKS Backup — weekly Vespa backup (runbook)

Goal: weekly, managed backup of **only the Vespa state that can't be cheaply
rebuilt**, using **Azure Backup for AKS** (the `azure-aks-backup` cluster
extension + a Backup vault). Managed identity is used throughout, so there is
**no service-principal secret to expire** — which is exactly what broke the
previous self-managed Velero (`AADSTS7000215: invalid client secret`).

## What gets backed up

Postgres (the real source of truth) is **external + Azure-managed**
(`darwin-postgres` flexible server) and is backed up separately. In-cluster,
the only durable, not-cheaply-rebuildable state is Vespa.

Selected by the label `backup=vespa` (already applied):

| PVC | Size | Why |
|---|---|---|
| `vespa-var-vespa-content-{0,1,2}` | 3×100Gi | the search index (rebuild = full re-index, hours–days) |
| `vespa-var1-vespa-configserver-{0,1,2}` | 3×5Gi | deployed app package/schema — pairs with content for a turnkey restore |

Everything else is intentionally excluded: model caches (`indexing-model-*`,
`inference-model-pvc`) re-download on boot; `vespa-logs*` are logs;
`vespa-workspace*` is deploy scratch; `dynamic-pvc`/`file-connector-pvc` are
unmounted/unused (Postgres+Blob file store); Redis is cache.

**Consistency caveat:** CSI/disk snapshots are point-in-time *per volume*, not
coordinated across the 3 content nodes. Vespa's redundancy + startup recovery
usually makes a restore fine, but treat this as a **fast-DR accelerator** — the
authoritative recovery path is still re-indexing from Postgres.

## Environment (real values)

```
SUBSCRIPTION   202e5d15-5356-4826-bc61-ebd449d12e34   (Internal-Production-EA)
TENANT         d8353d2a-b153-4d17-8827-902c51f72357
AKS            name=darwin  rg=darwin  nodeRG=MC_darwin_darwin_westeurope  identity=SystemAssigned
LOCATION       westeurope
NAMESPACE      darwin
PVC SELECTOR   backup=vespa   (already labeled on the 6 PVCs)
```

### Two hard constraints from this environment

1. **The `darwin` RG has a `CanNotDelete` lock.** Backup retention *deletes*
   old snapshots + the snapshot-RG contents, so the **snapshot resource group
   and the backup storage account must live in a SEPARATE, unlocked RG**
   (below: `darwin-backup`). Never point the snapshot RG at `darwin`.
2. **You (PIM Contributor) cannot do role assignments or Trusted Access
   bindings.** Steps tagged **[OWNER]** require Owner / User Access
   Administrator. Contributor-only steps are tagged **[CONTRIB]**.

## Steps

```bash
SUB=202e5d15-5356-4826-bc61-ebd449d12e34
LOC=westeurope
BKP_RG=darwin-backup          # unlocked RG for backup artifacts + snapshots
SA=darwinaksbkp$RANDOM        # storage account (globally-unique, <=24 chars, lowercase)
VAULT=darwin-bkp-vault
AKS_ID=$(az aks show -g darwin -n darwin --query id -o tsv)

# 1. [CONTRIB] providers
az provider register --namespace Microsoft.KubernetesConfiguration
az provider register --namespace Microsoft.DataProtection

# 2. [CONTRIB] unlocked RG + storage for backup metadata (Velero store) + snapshots
az group create -n $BKP_RG -l $LOC
az storage account create -n $SA -g $BKP_RG -l $LOC --sku Standard_LRS --min-tls-version TLS1_2
az storage container create -n aks-backup --account-name $SA --auth-mode login

# 3. [CONTRIB] install the managed backup extension (runs Velero in dataprotection-microsoft ns)
az k8s-extension create -g darwin -c darwin --cluster-type managedClusters \
  --extension-type Microsoft.DataProtection.Kubernetes --name azure-aks-backup \
  --release-train stable --scope cluster \
  --config blobContainer=aks-backup storageAccount=$SA \
           storageAccountResourceGroup=$BKP_RG storageAccountSubscriptionId=$SUB

# 4. [OWNER] grant the extension MSI write access to the backup storage account
EXT_MSI=$(az k8s-extension show -g darwin -c darwin --cluster-type managedClusters \
  -n azure-aks-backup --query aksAssignedIdentity.principalId -o tsv)
SA_ID=$(az storage account show -n $SA -g $BKP_RG --query id -o tsv)
az role assignment create --assignee $EXT_MSI --role "Storage Account Contributor" --scope $SA_ID

# 5. [CONTRIB] Backup vault (system-assigned identity)
az dataprotection backup-vault create -g $BKP_RG --vault-name $VAULT -l $LOC \
  --type SystemAssigned \
  --storage-settings datastore-type="VaultStore" type="LocallyRedundant"
VAULT_ID=$(az dataprotection backup-vault show -g $BKP_RG --vault-name $VAULT --query id -o tsv)
VAULT_MSI=$(az dataprotection backup-vault show -g $BKP_RG --vault-name $VAULT --query identity.principalId -o tsv)

# 6. [OWNER] Trusted Access rolebinding (cluster <-> vault) — creates a role assignment
az aks trustedaccess rolebinding create -g darwin --cluster-name darwin \
  -n darwin-backup-binding --source-resource-id $VAULT_ID \
  --roles Microsoft.DataProtection/backupVaults/backup-operator

# 7. [OWNER] vault MSI roles: read the cluster, snapshot the disks, write blobs
az role assignment create --assignee $VAULT_MSI --role "Reader" --scope $AKS_ID
az role assignment create --assignee $VAULT_MSI --role "Disk Snapshot Contributor" \
  --scope $(az group show -n $BKP_RG --query id -o tsv)
az role assignment create --assignee $VAULT_MSI --role "Storage Account Contributor" --scope $SA_ID
# extension MSI also needs read on the source disks' node RG:
az role assignment create --assignee $EXT_MSI --role "Contributor" \
  --scope $(az group show -n MC_darwin_darwin_westeurope --query id -o tsv)

# 8. [CONTRIB] WEEKLY policy (edit the default template's schedule -> weekly + retention)
az dataprotection backup-policy get-default-policy-template \
  --datasource-type AzureKubernetesService > policy.json
# edit policy.json: trigger schedule -> "R/2024-01-07T02:00:00+00:00/P1W" (weekly, Sun 02:00),
#   default retention e.g. 4–8 weeks. Then:
az dataprotection backup-policy create -g $BKP_RG --vault-name $VAULT \
  -n weekly-vespa --policy policy.json

# 9. [CONTRIB] backup instance — namespace=darwin, label=backup=vespa, snapshots ON,
#    snapshot RG = the UNLOCKED $BKP_RG (NOT darwin)
POLICY_ID=$(az dataprotection backup-policy show -g $BKP_RG --vault-name $VAULT \
  -n weekly-vespa --query id -o tsv)
az dataprotection backup-instance initialize-backupconfig \
  --datasource-type AzureKubernetesService \
  --label-selectors backup=vespa --included-namespaces darwin \
  --snapshot-volumes true --include-cluster-scope-resources false > backupconfig.json
az dataprotection backup-instance initialize \
  --datasource-id $AKS_ID --datasource-location $LOC \
  --datasource-type AzureKubernetesService --policy-id $POLICY_ID \
  --backup-configuration ./backupconfig.json \
  --friendly-name darwin-vespa-weekly \
  --snapshot-resource-group-name $BKP_RG > backupinstance.json
az dataprotection backup-instance create -g $BKP_RG --vault-name $VAULT \
  --backup-instance backupinstance.json

# 10. [CONTRIB] trigger an on-demand backup to validate end-to-end
az dataprotection backup-instance adhoc-backup -g $BKP_RG --vault-name $VAULT \
  --backup-instance-name <instance-name-from-step-9> \
  --rule-name <backup-rule-name-from-policy>
```

## Validation

```bash
# extension healthy + Velero pods up
az k8s-extension show -g darwin -c darwin --cluster-type managedClusters -n azure-aks-backup \
  --query provisioningState -o tsv
kubectl get pods -n dataprotection-microsoft
# backup instance status
az dataprotection backup-instance list -g darwin-backup --vault-name darwin-bkp-vault \
  --query "[].{name:name, status:properties.protectionStatus.status}" -o table
```

## Owner hand-off (the [OWNER] steps, minimal set)

Hand these to whoever holds Owner / User Access Administrator on sub
`202e5d15-…`. All scoped to the backup artifacts, none touch the locked
`darwin` RG's existing resources:

- `Storage Account Contributor` for the **extension MSI** on the backup SA (step 4)
- `aks trustedaccess rolebinding` cluster↔vault (step 6)
- vault MSI: `Reader` on AKS, `Disk Snapshot Contributor` on `darwin-backup`,
  `Storage Account Contributor` on the backup SA (step 7)
- extension MSI: `Contributor` on `MC_darwin_darwin_westeurope` (to snapshot the
  source disks) (step 7)

## Notes

- The 6 source PVCs are already labeled `backup=vespa`
  (`kubectl get pvc -n darwin -l backup=vespa`). Adjust scope by relabeling.
- Old self-managed Velero (namespace `velero`, CRDs, schedule `daily-backups`,
  373 backup CRs) and its 130 stale Azure disk snapshots in
  `MC_darwin_darwin_westeurope` were removed on 2026-06-11. The old backup
  *blobs* in storage account `darwinaksbackup` are NOT deleted by that — drop
  that storage account separately if it's no longer needed.
</content>
</invoke>
