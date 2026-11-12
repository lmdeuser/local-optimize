# DEB и OBS

## Что входит в пакет

`local-optimize 1.0.1-0ubuntu5` для Ubuntu 26.04/amd64 устанавливает:

- приложение и переносимое Python 3.14/XPU-окружение в `/usr/lib/local-optimize`;
- CLI `/usr/bin/local-optimize`;
- pinned E5 и Qwen reranker в `/usr/lib/local-optimize/models`;
- pinned manifest/blobs `qwen2.5-coder:7b` в стандартное хранилище `/usr/share/ollama/.ollama/models`;
- документацию и неактивные user-unit шаблоны.

После установки **не выполняются** `pip`, создание venv или скачивание моделей. Все эти операции происходят только во время сборки. Пакет содержит бинарные wheels, поэтому не является архитектурно независимым и не предназначен для Ubuntu 24.04.

Системными prerequisites остаются Intel driver/OpenCL ICD, PostgreSQL 18/pgvector и установленные Ollama binary/service. Пакет не создаёт БД, секреты, projects registry или конфигурацию агентов и не включает user services до настройки `secrets.env`. Корневая файловая система с runtime должна находиться на SSD.

## Контролируемая сборка

Единственный разрешённый локальный packaging entrypoint — проектный runner `packaging/run-obs-source-local.sh`. Он формирует только `.dsc`, основной orig tar и Debian tar во временном каталоге, публикует их как GitHub Release assets и удаляет временные файлы. Регистрировать новые GitHub-hosted или self-hosted runners для проекта запрещено.

Бинарная сборка и проверка пакета выполняются только в OBS. Локальный запуск `packaging/build-deb.sh` (включая `SOURCE_ONLY=1`), `dpkg-buildpackage`, подготовка runtime и `lintian` для локально собранных пакетов запрещены. Локальные `.deb` и runtime-компоненты не являются релизными артефактами и не должны загружаться в OBS.

Релиз принимается только со статусом OBS `succeeded` для `xUbuntu_26.04`/`x86_64`. В локальный workspace можно скачивать готовые пакеты OBS. Случайно созданные build/cache/diagnostic-каталоги и временные worktree удаляются.

`packaging/runtime-policy.json` фиксирует revisions и digest; `runtime/manifest.json` в готовом пакете содержит SHA-256 payload. Компоненты ограничены 512 МиБ до сжатия; большие файлы разбиваются и собираются с проверкой SHA-256 во время облачной сборки. Секреты, Git-игнорируемые заметки и произвольные untracked-файлы не должны попадать в release inputs.

## Установка DEB

После скачивания готового пакета, собранного OBS со статусом `succeeded`, администратор может установить его:

```bash
sudo apt install ./artifacts/debian/local-optimize_1.0.1-0ubuntu5_amd64.deb
```

Затем настройте секреты и реестр проектов по `/usr/share/doc/local-optimize/docs/installation.md`, выполните `local-optimize migrate`, проверьте health и только после этого включите user services. Не создавайте отдельное окружение в `~/.local/share/local-optimize/venv`: CLI использует `/usr/lib/local-optimize/venv`. `local-optimize index` сохраняет каталог вызова через `LOCAL_OPTIMIZE_CALLER_DIR`; при необходимости явный контекст можно задать через `--working-directory`.

## OBS

Текущая цель: `home:dwolfF:local-optimize/local-optimize`, репозиторий `xUbuntu_26.04`, архитектура `x86_64`. Требования к релизу и команды проверки статуса находятся в `obs/README.md`. Проектный runner формирует только source assets; успешным релизом считается только OBS `succeeded`.

## Подключение APT-репозитория

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

После обновления с версии 1.0.0-1 проверьте пользовательские drop-in: старый `ExecStart` из Git-каталога нужно удалить и использовать packaged units. Агенты мигрируются отдельно на `/usr/bin/local-optimize mcp`; privacy-блокировка cloud-capable клиентов сохраняется.
