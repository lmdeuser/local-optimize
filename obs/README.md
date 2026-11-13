# openSUSE Build Service

Target project and package:

- Project: `home:dwolfF:local-optimize`
- Package: `local-optimize`
- Build target: `xUbuntu_26.04` on `x86_64`

The package targets Ubuntu 26.04/amd64 and provisions Python 3.14 Intel XPU dependencies (`/var/lib/local-optimize/venv`), HuggingFace models (`/var/lib/local-optimize/models`), and the Ollama model on the target host during package configuration (`local-optimize sync`). Ubuntu 24.04 is not a compatible target for this runtime.

## Controlled build policy

The only permitted local packaging entrypoint is the project-local runner:

```bash
packaging/run-obs-source-local.sh
```

It creates the `.dsc`, main orig tar, and Debian tar in a temporary directory, publishes them as GitHub Release assets, and removes the temporary files. Never register new GitHub-hosted or self-hosted runners for this project.

OBS is the binary build and package-verification authority. Never run `packaging/build-deb.sh` (including `SOURCE_ONLY=1`), `dpkg-buildpackage`, runtime staging, or `lintian` on locally built packages. Never upload locally generated DEB or runtime component archives. Remove accidental build artifacts, caches, diagnostic directories, and temporary worktrees.

`obs/_service` downloads the published source assets (`dsc`, `orig.tar.gz`, `debian.tar.xz`) into OBS. Multi-part `orig-runtime-*` weight/venv components are no longer bundled in the `.deb`. Secrets, agent configuration, Git-ignored notes, and unrelated untracked files must never enter release inputs.

A release is accepted only when OBS reports `succeeded`; a local `.deb` is never a substitute.

## Create or update OBS metadata

These commands require the configured OBS account and explicit authorization to modify the project:

```bash
osc -A https://api.opensuse.org api -X PUT \
  -f obs/project.xml /source/home:dwolfF:local-optimize/_meta
osc -A https://api.opensuse.org api -X PUT \
  -f obs/package.xml /source/home:dwolfF:local-optimize/local-optimize/_meta
```

## Inspect the cloud build

The project-local runner publishes source assets, and the OBS source service downloads them. Do not replace this with an `osc checkout` workflow that copies arbitrary local archives into OBS.

Inspect cloud results and logs:

```bash
osc -A https://api.opensuse.org results \
  home:dwolfF:local-optimize local-optimize
osc -A https://api.opensuse.org remotebuildlog \
  home:dwolfF:local-optimize local-optimize xUbuntu_26.04 x86_64
```

The build is accepted only when OBS reports `succeeded`. Only then may its ready packages be downloaded into the local workspace or installed from the APT repository.

## Repository

- Web UI: <https://build.opensuse.org/project/show/home:dwolfF:local-optimize>
- APT root: <https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_26.04/>

## Connect the APT repository

```bash
sudo install -d -m 0755 /usr/share/keyrings
curl -fsSL https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_26.04/Release.key \
  | sudo gpg --dearmor --yes --output /usr/share/keyrings/local-optimize-obs.gpg
sudo chmod 0644 /usr/share/keyrings/local-optimize-obs.gpg
echo 'deb [signed-by=/usr/share/keyrings/local-optimize-obs.gpg] https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_26.04/ /' \
  | sudo tee /etc/apt/sources.list.d/local-optimize-obs.list
sudo apt update
sudo apt install local-optimize
```

After installation, configure secrets/projects and run migrations before enabling the user units. Existing allowed agents must point to `/usr/bin/local-optimize mcp`; direct RAG registration of cloud-capable clients remains prohibited.
