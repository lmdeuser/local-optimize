# openSUSE Build Service

Target project and package:

- Project: `home:dwolfF:local-optimize`
- Package: `local-optimize`
- Build target: `xUbuntu_26.04` on `x86_64`

The package contains compiled Python 3.14/XPU dependencies and pinned models. It is intentionally specific to Ubuntu 26.04/amd64; Ubuntu 24.04 is not a compatible target for this payload.

## Build source artifacts locally

From the Git repository root:

```bash
packaging/build-deb.sh
lintian artifacts/debian/local-optimize_1.0.0-2_amd64.deb
```

The script copies only tracked source files, then creates the complete runtime in a temporary build directory. Outputs in `artifacts/debian/` include:

- `local-optimize_1.0.0-2_amd64.deb`
- `local-optimize_1.0.0.orig.tar.gz`
- `local-optimize_1.0.0-2.dsc`
- `local-optimize_1.0.0-2.debian.tar.xz`
- build information and changes files

The orig tar intentionally contains the generated venv and pinned model payload, so these artifacts are large. Upload only the source tarball, Debian tarball and `.dsc` for an OBS source build. Never upload secrets, agent configuration or unrelated local artifacts.

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
cp artifacts/debian/local-optimize_1.0.0.orig.tar.gz \
   artifacts/debian/local-optimize_1.0.0-2.dsc \
   artifacts/debian/local-optimize_1.0.0-2.debian.tar.xz \
   artifacts/obs/local-optimize/
cd artifacts/obs/local-optimize
osc add local-optimize_1.0.0.orig.tar.gz \
        local-optimize_1.0.0-2.dsc \
        local-optimize_1.0.0-2.debian.tar.xz
osc commit -m "Build local-optimize 1.0.0-2 for Ubuntu 26.04"
```

Inspect results and logs:

```bash
osc -A https://api.opensuse.org results \
  home:dwolfF:local-optimize local-optimize
osc -A https://api.opensuse.org buildlog \
  home:dwolfF:local-optimize local-optimize xUbuntu_26.04 x86_64
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
