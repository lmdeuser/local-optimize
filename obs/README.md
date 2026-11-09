# openSUSE Build Service

Target project and package:

- Project: `home:dwolfF:local-optimize`
- Package: `local-optimize`
- Build target: `xUbuntu_24.04` on `x86_64`

The project metadata uses the upstream `Ubuntu:24.04/standard` repository. Python 3.12 in Ubuntu 24.04 satisfies the package's minimum Python version.

## Build source artifacts locally

From the Git repository root:

```bash
packaging/build-deb.sh
```

The script copies only tracked and non-ignored working-tree files into a temporary build directory under `artifacts/`. It leaves these unsigned outputs in `artifacts/debian/`:

- `local-optimize_1.0.0-1_all.deb`
- `local-optimize_1.0.0.orig.tar.gz`
- `local-optimize_1.0.0-1.dsc`
- `local-optimize_1.0.0-1.debian.tar.xz`
- build information and changes files

Only the source tarball, Debian tarball and `.dsc` are uploaded for an OBS source build. Do not upload local binaries, `.env`, models, secrets or agent configuration.

## Create or update OBS metadata

The following commands require the configured OBS account and permission to modify the project:

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
   artifacts/debian/local-optimize_1.0.0-1.dsc \
   artifacts/debian/local-optimize_1.0.0-1.debian.tar.xz \
   artifacts/obs/local-optimize/
cd artifacts/obs/local-optimize
osc add local-optimize_1.0.0.orig.tar.gz \
        local-optimize_1.0.0-1.dsc \
        local-optimize_1.0.0-1.debian.tar.xz
osc commit -m "Build local-optimize 1.0.0-1 for Ubuntu 24.04"
```

Inspect results and logs:

```bash
osc -A https://api.opensuse.org results \
  home:dwolfF:local-optimize local-optimize
osc -A https://api.opensuse.org buildlog \
  home:dwolfF:local-optimize local-optimize xUbuntu_24.04 x86_64
```

The build is accepted only when OBS reports `succeeded`. A local `.deb` alone is not proof of an OBS build.

## Repository

- Web UI: <https://build.opensuse.org/project/show/home:dwolfF:local-optimize>
- APT root: <https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_24.04/>

## Connect the APT repository

The repository publishes `Release`, `Release.key` and a package index. Use the signing key instead of disabling APT verification:

```bash
sudo install -d -m 0755 /usr/share/keyrings
curl -fsSL https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_24.04/Release.key \
  | sudo gpg --dearmor --yes --output /usr/share/keyrings/local-optimize-obs.gpg
sudo chmod 0644 /usr/share/keyrings/local-optimize-obs.gpg
echo 'deb [signed-by=/usr/share/keyrings/local-optimize-obs.gpg] https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_24.04/ /' \
  | sudo tee /etc/apt/sources.list.d/local-optimize-obs.list
sudo apt update
sudo apt install local-optimize
```

These commands are instructions for the user; the packaging task does not run them or modify the host APT configuration.

Agent installation and migration to an updated engine are outside this packaging task. Do not enable the user units until model, database, secrets and health setup is complete.
