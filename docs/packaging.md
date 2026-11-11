# DEB и OBS

## Что входит в пакет

`local-optimize 1.0.1-0ubuntu2` для Ubuntu 26.04/amd64 устанавливает:

- приложение и переносимое Python 3.14/XPU-окружение в `/usr/lib/local-optimize`;
- CLI `/usr/bin/local-optimize`;
- pinned E5 и Qwen reranker в `/usr/lib/local-optimize/models`;
- pinned manifest/blobs `qwen2.5-coder:7b` в стандартное хранилище `/usr/share/ollama/.ollama/models`;
- документацию и неактивные user-unit шаблоны.

После установки **не выполняются** `pip`, создание venv или скачивание моделей. Все эти операции происходят только во время сборки. Пакет содержит бинарные wheels, поэтому не является архитектурно независимым и не предназначен для Ubuntu 24.04.

Системными prerequisites остаются Intel driver/OpenCL ICD, PostgreSQL 18/pgvector и установленные Ollama binary/service. Пакет не создаёт БД, секреты, projects registry или конфигурацию агентов и не включает user services до настройки `secrets.env`. Корневая файловая система с runtime должна находиться на SSD.

## Локальная сборка

Требуются `build-essential`, `debhelper`, Python 3.14 с `venv`, `python3-pip`, `lintian`, сеть и достаточное место для автономного runtime. Из корня репозитория:

```bash
packaging/build-deb.sh
lintian artifacts/debian/local-optimize_1.0.1-0ubuntu2_amd64.deb
```

Скрипт копирует только tracked-файлы, создаёт venv и скачивает/копирует модели в изолированный каталог `artifacts/deb-build.*`. `TMPDIR` и pip cache по умолчанию находятся там же и удаляются после сборки. Можно заранее задать `PIP_CACHE_DIR` и `OLLAMA_MODELS_SOURCE` для повторного использования проверенных входов. `packaging/runtime-policy.json` фиксирует revisions и digest; `runtime/manifest.json` в пакете содержит SHA-256 payload.

Результат включает `_amd64.deb`, `.dsc`, основной orig tar, несколько `orig-runtime-*.tar.gz` и Debian tar. Компоненты ограничены 512 МиБ до сжатия; большие файлы разбиваются и собираются с проверкой SHA-256 на этапе сборки. Это позволяет OBS принимать payload отдельными запросами вместо одного архива 7,8 ГБ. Локальные секреты, Git-игнорируемые заметки и произвольные untracked-файлы в пакет не попадают. Для публикации используйте `SOURCE_ONLY=1 packaging/build-deb.sh`, чтобы бинарный DEB собирал OBS.

## Установка DEB

После проверки пакета администратор может установить его:

```bash
sudo apt install ./artifacts/debian/local-optimize_1.0.1-0ubuntu2_amd64.deb
```

Затем настройте секреты и реестр проектов по `/usr/share/doc/local-optimize/docs/installation.md`, выполните `local-optimize migrate`, проверьте health и только после этого включите user services. Не создавайте отдельное окружение в `~/.local/share/local-optimize/venv`: CLI использует `/usr/lib/local-optimize/venv`.

## OBS

Текущая цель: `home:dwolfF:local-optimize/local-optimize`, репозиторий `xUbuntu_26.04`, архитектура `x86_64`. Команды публикации и проверки находятся в `obs/README.md`. Локальная сборка не считается успешной OBS-сборкой: требуется статус `succeeded`.

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
