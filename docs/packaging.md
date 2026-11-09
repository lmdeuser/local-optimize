# DEB и OBS

## Что входит в пакет

`local-optimize 1.0.0-1` устанавливает:

- приложение в `/usr/lib/local-optimize`;
- CLI `/usr/bin/local-optimize`;
- документацию в `/usr/share/doc/local-optimize`;
- неактивные user-unit шаблоны `gpu-rag-server.service` и `watchdog-indexer.service`.

Пакет **не** скачивает модели, не создаёт venv, БД, секреты или конфигурацию агентов, не включает и не запускает службы. Установка и миграция агентов на обновлённый движок — отдельная задача.

## Локальная сборка

Требуются `build-essential`, `debhelper`, `python3` и `lintian`. Из корня репозитория:

```bash
packaging/build-deb.sh
lintian artifacts/debian/local-optimize_1.0.0-1_all.deb
```

Скрипт использует только tracked и non-ignored файлы. Сборка выполняется во временном каталоге внутри `artifacts/`, поэтому модели, секреты и локальные конфигурации не попадают в пакет. Результат: бинарный `.deb`, исходный `.dsc` и tar-файлы в `artifacts/debian/`.

## Установка DEB

После проверки пакета его можно установить администратору системы:

```bash
sudo apt install ./artifacts/debian/local-optimize_1.0.0-1_all.deb
```

Эта команда приведена для ручного применения пользователем и не выполняется во время сборки. Она не настраивает агентов и не запускает engine.

## Python-зависимости

Зависимости PyPI не встроены в DEB. Создайте отдельное пользовательское окружение:

```bash
python3 -m venv ~/.local/share/local-optimize/venv
~/.local/share/local-optimize/venv/bin/python -m pip install --upgrade pip
~/.local/share/local-optimize/venv/bin/python -m pip install --no-deps \
  -r /usr/lib/local-optimize/deploy/intel-xpu-requirements.txt
~/.local/share/local-optimize/venv/bin/python -m pip install \
  -r /usr/lib/local-optimize/requirements.txt
```

`LOCAL_OPTIMIZE_VENV` позволяет выбрать другое окружение. Далее настройте PostgreSQL, секреты и модели по `/usr/share/doc/local-optimize/docs/installation.md`; вместо корня Git-репозитория используйте `/usr/lib/local-optimize`.

## Команды

```text
local-optimize migrate        # миграции БД, не миграция агентов
local-optimize server
local-optimize watchdog
local-optimize mcp
local-optimize lmstudio-mcp
local-optimize index
local-optimize version
```

Проверка CLI:

```bash
local-optimize --help
local-optimize version
```

Systemd units устанавливаются как шаблоны. Не выполняйте `systemctl --user enable --now`, пока не завершены настройка и health-проверки.

## OBS

Текущая цель: `home:dwolfF:local-optimize/local-optimize`, репозиторий `xUbuntu_24.04`, архитектура `x86_64`. Команды создания проекта, загрузки исходников и проверки результата находятся в `obs/README.md`.

- Web UI: <https://build.opensuse.org/project/show/home:dwolfF:local-optimize>
- APT root: <https://download.opensuse.org/repositories/home:/dwolfF:/local-optimize/xUbuntu_24.04/>

Публикация DEB вручную и сборка OBS являются разными проверками. Перед использованием APT-репозитория убедитесь, что OBS сообщает `succeeded`.

## Подключение APT-репозитория

Репозиторий публикует `Release`, `Release.key` и индекс пакетов. Подключение выполняется с проверкой подписи, без `trusted=yes`:

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

Это команды для самостоятельного применения пользователем. В рамках сборки они не выполняются и не изменяют APT-конфигурацию текущего хоста. После установки настройте runtime по `debian/README.Debian`; агенты и службы автоматически не мигрируются и не запускаются.
