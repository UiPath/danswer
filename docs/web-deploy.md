# Deploying the web image (`danswer-web-server`)

**TL;DR — do NOT use `build-deploy.sh deploy web` on an Apple-Silicon Mac.** The
web `next build` segfaults under Docker's amd64 emulation (see below). Build the
image natively on Azure with `az acr build`, copy it into the prod registry, then
bump the tag and apply.

The **backend** image is unaffected — `k8s/scripts/build-deploy.sh deploy backend`
works fine locally (no V8/SWC compile).

---

## Why the local web build fails

`docker build --platform linux/amd64 … next build` crashes with
`Next.js build worker exited … signal SIGSEGV` ~11s into the compile, on Apple
Silicon. As of 2026‑06‑11 this happens **even with Docker's Apple Virtualization
framework + Rosetta enabled** (it used to work with Rosetta; a Docker
Desktop / macOS / containerd-snapshotter change regressed it). None of these fix
it: build-cache prune, Docker restart, disabling the containerd image store,
`next.config` worker tweaks. `sharp` is a runtime dep, so you also can't dodge it
by mixing a glibc builder with the alpine runtime (libc mismatch).

So: **build off the Mac.**

---

## Registries & subscriptions

| Registry | Subscription | Purpose |
|---|---|---|
| `darwinacr.azurecr.io` | `Internal-Production-EA` (`202e5d15-…`), RG `darwin` | **Build here** (native amd64, same sub as the AKS) |
| `sfbrdevhelmweacr.azurecr.io` | a different sub (`d10f042e-…`) | **Prod pull registry** — what `k8s/overlays/prod` references |

Image tags follow `vha-N` (bump from the current `newTag` in
`k8s/overlays/prod/kustomization.yaml`).

---

## Build + deploy steps

Set `TAG` to the next `vha-N` (current web tag + 1):

```bash
TAG=vha-79   # <- bump from kustomization.yaml

# 1. Build natively on Azure (no emulation, no SIGSEGV).
#    Requires Contributor RBAC on darwinacr — see "Permissions" below.
az acr build --registry darwinacr \
  --image danswer/danswer-web-server:$TAG \
  --file web/Dockerfile web

# 2. Copy the image into the prod registry (pure blob copy — safe on the Mac,
#    no execution). darwinacr login via AAD; sfbrdevhelmweacr via admin creds.
az acr login --name darwinacr
docker login sfbrdevhelmweacr.azurecr.io -u "$ACR_USERNAME" -p "$ACR_PASSWORD"   # creds from ~/.zshrc
docker pull  --platform linux/amd64 darwinacr.azurecr.io/danswer/danswer-web-server:$TAG
docker tag   darwinacr.azurecr.io/danswer/danswer-web-server:$TAG \
             sfbrdevhelmweacr.azurecr.io/danswer/danswer-web-server:$TAG
docker push  sfbrdevhelmweacr.azurecr.io/danswer/danswer-web-server:$TAG

# 3. Point the prod overlay at the new tag and apply (context must be `darwin`).
#    Edit k8s/overlays/prod/kustomization.yaml: danswer-web-server newTag -> $TAG
kubectl apply -k k8s/overlays/prod

# 4. Verify.
bash k8s/scripts/build-deploy.sh verify        # live tags == manifest + pod health
kubectl get pods -n darwin -l app=web-server   # expect 2/2 Running on $TAG
```

Live sanity check (the app is OIDC-gated, so curl `/auth/login` — served by our
Next app — and confirm the build markers are present):

```bash
curl -s https://darwin.westeurope.cloudapp.azure.com/auth/login | grep -o "darwin-theme"
```

Finally, commit the `kustomization.yaml` tag bump (the script does not auto-commit it).

---

## Permissions

- **`az acr build` on darwinacr** needs ARM `…/registries/listBuildSourceUploadUrl/action`
  — i.e. **Contributor** on the registry. The `~/.zshrc` `ACR_USERNAME`/`ACR_PASSWORD`
  are registry **push admin** only; they do **not** satisfy this.
- Today this is obtained via a **PIM time-bound activation** of Contributor
  (it expires — re-activate per build session).
- **Pushing to `sfbrdevhelmweacr`** uses the admin creds in `~/.zshrc` (no ARM/PIM).

### Optional: unattended / CI builds via a service principal

So web deploys don't depend on a human's PIM activation, an **admin** (with Entra
app-registration rights + `User Access Administrator`/`Owner`) creates an SP:

```bash
az ad sp create-for-rbac --name darwin-web-ci \
  --role Contributor \
  --scopes /subscriptions/202e5d15-5356-4826-bc61-ebd449d12e34/resourceGroups/darwin/providers/Microsoft.ContainerRegistry/registries/darwinacr
# -> store appId / password / tenant as CI secrets
```

Then in CI (or locally):

```bash
az login --service-principal -u <appId> -p <password> --tenant <tenant>
az acr build --registry darwinacr --image danswer/danswer-web-server:$TAG -f web/Dockerfile web
# ... then the copy + apply steps above
```

> Note: the repo's existing `.github/workflows/docker-build-push-web-container-on-tag.yml`
> pushes to **Docker Hub** (upstream Onyx), not this ACR — it is not wired to this
> deploy path.
