# openSUSE Build Service

Target project and package:

- Project: `home:dwolfF:local-optimize`
- Package: `local-optimize`
- Build target: `xUbuntu_26.04` on `x86_64`

The package contains compiled Python 3.14/XPU dependencies and pinned models. It is intentionally specific to Ubuntu 26.04/amd64; Ubuntu 24.04 is not a compatible target for this payload.

## Build source artifacts locally

From the Git repository root:

```bash
SOURCE_ONLY=1 packaging/build-deb.sh
```

The script copies only tracked source files, then creates the complete runtime in a temporary build directory. `SOURCE_ONLY=1` avoids a redundant local binary build; OBS builds the `.deb` on its workers. Outputs in `artifacts/debian/` include:

- `local-optimize_1.0.1.orig.tar.gz` with source and manifests;
- multiple `local-optimize_1.0.1.orig-runtime-*.tar.gz` components with the generated venv and pinned models;
- `local-optimize_1.0.1-0ubuntu3.dsc` and `local-optimize_1.0.1-0ubuntu3.debian.tar.xz`;
- source changes files.

Components are limited to 512 MiB before compression so OBS does not have to accept one multi-gigabyte request. Oversized files are split and reassembled with SHA-256 verification during the OBS build. Upload the main orig tar, every runtime component, Debian tar and `.dsc`. Never upload secrets, agent configuration or unrelated local artifacts.

For a separate local binary verification run `packaging/build-deb.sh` without `SOURCE_ONLY` and then `lintian artifacts/debian/local-optimize_1.0.1-0ubuntu3_amd64.deb`.

## Create or update OBS metadata

These commands require the configured OBS account and explicit authorization to modify the project:

```bash
osc -A https://api.opensuse.org api -X PUT \
  -f obs/project.xml /source/home:dwolfF:local-optimize/_meta
osc -A https://api.opensuse.org api -X PUT \
  -f obs/package.xml /source/home:dwolfF:local-optimize/local-optimize/_meta
```

## Upload a source package

```bash
mkdir -p artifacts/obs
osc -A https://api.opensuse.org checkout \
  home:dwolfF:local-optimize/local-optimize \
  -o artifacts/obs/local-optimize
cp artifacts/debian/local-optimize_1.0.1*.tar.gz \
   artifacts/debian/local-optimize_1.0.1-0ubuntu3.dsc \
   artifacts/debian/local-optimize_1.0.1-0ubuntu3.debian.tar.xz \
   artifacts/obs/local-optimize/
cd artifacts/obs/local-optimize
osc add local-optimize_1.0.1*.tar.gz \
        local-optimize_1.0.1-0ubuntu3.dsc \
        local-optimize_1.0.1-0ubuntu3.debian.tar.xz
osc commit -m "Build local-optimize 1.0.1-0ubuntu3 for Ubuntu 26.04"
```

Inspect results and logs:

```bash
osc -A https://api.opensuse.org results \
  home:dwolfF:local-optimize local-optimize
cd artifacts/obs/local-optimize
osc -A https://api.opensuse.org buildlog xUbuntu_26.04 x86_64
```

The build is accepted only when OBS reports `succeeded`. A local `.deb` alone is not proof of an OBS build.

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
