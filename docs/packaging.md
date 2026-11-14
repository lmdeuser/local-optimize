# DEB и OBS

## Что входит в пакет

`local-optimize 1.0.1-0ubuntu9` для Ubuntu 26.04/amd64 устанавливает компактный пакет (~150 КБ без гигабайтных бинарных payload-архивов):

- приложение, `requirements.lock` и `packaging/runtime-policy.json` в `/usr/lib/local-optimize`;
- CLI `/usr/bin/local-optimize`;
- документацию и неактивные user-unit шаблоны.

На целевой машине при установке (`postinst`) или вручную через `sudo local-optimize sync`:

- разворачивается общее Python 3.14/XPU-окружение в `/var/lib/local-optimize/venv` по `requirements.lock` (при неизменном `requirements.lock` при `apt upgrade` не переустанавливается);
- скачиваются и обновляются по upstream-метаданным модели E5 и Qwen reranker в `/var/lib/local-optimize/models` (веса `multilingual-e5-small` защищены от тихой перезаписи без `--allow-embedding-update`);
- проверяется и при необходимости обновляется `qwen2.5-coder:7b` в локальной службе Ollama (`127.0.0.1:11434`);
- при `apt upgrade` выполняются `systemctl --user daemon-reload` и `try-restart` для уже активных пользовательских служб, а `gpu-rag-server.service` запускает `local-optimize migrate` в `ExecStartPre`.

Автономного фонового `systemd`-таймера автообновления моделей нет: проверка и обновление выполняются только при `apt install`/`apt upgrade` или явном вызове `sudo local-optimize sync`. При отсутствии сети и наличии валидной локальной копии сохраняются текущие локальные модели; если модель или `venv` отсутствуют и не могут быть получены, установка завершается ошибкой (`postinst` требует полный рабочий runtime).

Системными prerequisites остаются Intel driver/OpenCL ICD, PostgreSQL 18/pgvector и запущенный Ollama binary/service. Пакет не создаёт БД, секреты, projects registry или конфигурацию агентов и не включает user services до настройки `secrets.env`. Каталоги `/var/lib/local-optimize` и `/usr/share/ollama` должны находиться на SSD.

## Контролируемая сборка

Единственный разрешённый локальный packaging entrypoint — проектный runner `packaging/run-obs-source-local.sh`. Он формирует только `.dsc`, основной orig tar и Debian tar во временном каталоге, публикует их как GitHub Release assets и удаляет временные файлы. Регистрировать новые GitHub-hosted или self-hosted runners для проекта запрещено.

Бинарная сборка и проверка пакета выполняются только в OBS. Локальный запуск `packaging/build-deb.sh` (включая `SOURCE_ONLY=1`), `dpkg-buildpackage`, подготовка runtime и `lintian` для локально собранных пакетов запрещены. Локальные `.deb` не являются релизными артефактами и не должны загружаться в OBS.

Релиз принимается только со статусом OBS `succeeded` для `xUbuntu_26.04`/`x86_64`. В локальный workspace можно скачивать готовые пакеты OBS. Случайно созданные build/cache/diagnostic-каталоги и временные worktree удаляются.

`packaging/runtime-policy.json` фиксирует базовые ревизии, обязательные файлы моделей и политику синхронизации; `runtime/requirements.lock` фиксирует точные версии wheel-пакетов Intel XPU. Секреты, Git-игнорируемые заметки и произвольные untracked-файлы не должны попадать в release inputs.

## Установка DEB

После скачивания готового пакета, собранного OBS со статусом `succeeded`, администратор может установить его:

```bash
sudo apt install ./artifacts/debian/local-optimize_1.0.1-0ubuntu9_amd64.deb
```

Затем настройте секреты и реестр проектов по `/usr/share/doc/local-optimize/docs/installation.md`, выполните `local-optimize migrate`, проверьте health и только после этого включите user services. Не создавайте отдельное окружение в `~/.local/share/local-optimize/venv`: CLI использует `/var/lib/local-optimize/venv`. `local-optimize index` сохраняет каталог вызова через `LOCAL_OPTIMIZE_CALLER_DIR`; при необходимости явный контекст можно задать через `--working-directory`.

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
