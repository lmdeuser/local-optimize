# Установка и настройка

Для Ubuntu 26.04/amd64 рекомендуется готовый DEB из `docs/packaging.md`: он уже содержит Python 3.14/XPU-окружение, E5, Qwen reranker и модель `qwen2.5-coder:7b`. После его установки не выполняйте команды `pip`, создание venv или скачивание моделей из разделов ручной установки. CLI использует `/usr/lib/local-optimize/venv`, а runtime должен находиться на SSD.

Остальное руководство описывает ручной запуск для разработки и настройку, общую для обоих вариантов. Для DEB пропустите шаги создания окружения и скачивания моделей, замените команды `python create_schema.py` на `local-optimize migrate`, а `python gpu_rag_server.py` — на `local-optimize server`. PostgreSQL/pgvector, Intel driver/OpenCL ICD, Ollama binary/service и секреты остаются prerequisites.

## 1. Ограничения и требования

- ОС: Linux. Код использует `fcntl`, а службы рассчитаны на пользовательский `systemd`.
- Аппаратное ускорение: поддерживаемый Intel Arc с рабочим PyTorch XPU и OpenCL ICD. Production retrieval не имеет CPU/CUDA/ONNX fallback.
- Python 3.12 или новее: код использует синтаксис этих версий; текущий набор проверен на Python 3.14.4. Совместимость с другими версиями требует отдельной проверки.
- PostgreSQL с pgvector, отдельная БД `aiagent` и пользователь `aiagent`.
- Ollama на loopback-интерфейсе.
- Диск: зарезервируйте место для PyTorch XPU, E5, Qwen3 и Ollama-модели. Веса не входят в Git-репозиторий.

Проверьте базовые команды:

```bash
python3 --version
psql --version
ollama --version
systemctl --user --version
```

Драйвер Intel и pgvector устанавливаются штатными средствами вашего дистрибутива. Используйте документацию [PyTorch XPU](https://docs.pytorch.org/docs/stable/notes/get_start_xpu.html) и [pgvector](https://github.com/pgvector/pgvector); не подменяйте их пакетами из непроверенных источников.

## 2. Исходный код и Python-зависимости

```bash
git clone https://github.com/lmdeuser/local-optimize.git
cd local-optimize
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --no-deps -r deploy/intel-xpu-requirements.txt
python -m pip install -r requirements.txt
```

Первый `--no-deps` устанавливает зафиксированный Intel runtime. Последующий общий `requirements.txt` повторно ссылается на этот файл и разрешает остальные зависимости пакетов. Не заменяйте XPU-сборку PyTorch обычной CPU/CUDA-сборкой.

Проверьте окружение:

```bash
python -m pip check
python -c "import torch; assert torch.xpu.is_available(), 'Intel XPU is unavailable'"
```

Для разработки дополнительно:

```bash
python -m pip install -r requirements-dev.txt
```

## 3. PostgreSQL и pgvector

Команды ниже выполняет администратор PostgreSQL. Они создают отдельную роль и БД; не запускайте их повторно без проверки существующих объектов. Пароль вводится интерактивно и не остаётся в истории команд:

```bash
sudo -u postgres createuser --pwprompt aiagent
sudo -u postgres createdb --owner=aiagent aiagent
sudo -u postgres psql --dbname=aiagent --command='CREATE EXTENSION IF NOT EXISTS vector;'
```

Если роль и БД уже существуют, достаточно проверить владельца и расширение. Миграции не создают саму БД и не должны выполняться под суперпользователем в обычной эксплуатации.

## 4. Секреты и конфигурация

Создайте защищённый каталог и скопируйте только пример:

```bash
install -d -m 700 "$HOME/.config/local-optimize"
cp docs/examples/secrets.env.example "$HOME/.config/local-optimize/secrets.env"
chmod 600 "$HOME/.config/local-optimize/secrets.env"
```

Откройте файл в редакторе и замените все placeholders:

```bash
"${EDITOR:-vi}" "$HOME/.config/local-optimize/secrets.env"
```

Обязательные параметры:

| Параметр | Назначение |
| --- | --- |
| `DB_HOST`, `DB_PORT` | Адрес PostgreSQL; для локальной установки `127.0.0.1:5432`. |
| `DB_NAME`, `DB_USER` | `aiagent` / `aiagent`; installer намеренно проверяет имя БД. |
| `DB_PASSWORD` | Пароль роли PostgreSQL. |
| `DB_SSLMODE` | Один из `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`. |
| `RAG_BEARER_TOKEN` | Bearer-токен HTTP/MCP-клиентов. Обязателен для не-loopback bind и рекомендуется всегда. |

SSL-сертификаты при необходимости задаются через `DB_SSLROOTCERT`, `DB_SSLCERT`, `DB_SSLKEY`. Права каталога должны быть `0700`, файла — `0600`. Не копируйте реальный файл обратно в проект и не отправляйте его в Git. При программной записи значения используются двойные кавычки и escaping, совместимый с systemd `EnvironmentFile`: кавычки, краевые пробелы, `#`, backslash, `$`, backtick и Unicode сохраняются при round-trip.

`manage_secrets.py` позволяет интерактивно менять основные DB-параметры:

```bash
python manage_secrets.py DB_PASSWORD
```

Bearer-токен и SSL-пути редактируются непосредственно в защищённом файле. Сгенерируйте токен локальным криптографическим генератором, например `openssl rand -hex 32`; не используйте пример из документации.

### Реестр проектов

Без реестра или при `"projects": []` сервер работает без подключённых источников; `/readiness` по-прежнему проверяет только engine, PostgreSQL, миграции и схему. Каталог движка автоматически не индексируется и не становится проектом. Для подключения источников:

```bash
cp docs/examples/projects.json.example "$HOME/.config/local-optimize/projects.json"
chmod 600 "$HOME/.config/local-optimize/projects.json"
"${EDITOR:-vi}" "$HOME/.config/local-optimize/projects.json"
```

Замените placeholder-пути на существующие каталоги. `project_id` и `root_id` используются для явного контекста; абсолютные корни и права определяет только сервер. Проект с `"roots": {}` хранит rootless knowledge и доступен для явного/общего поиска, но не для индексации файлов.

Добавление, изменение и отключение источника выполняется атомарной заменой `projects.json` без restart сервера или watchdog. Не редактируйте файл частями во время чтения. Отключение прекращает наблюдение, но не удаляет знания. Отдельно зарегистрированный вложенный корень принадлежит только самому глубокому project/root: родительский watcher его не индексирует. Добавление такого корня меняет `context_fingerprint` родителя и инвалидирует его search/local-answer caches; независимый sibling не меняет старый контекст.

Обычный запрос требует explicit `project_id`/`root_id` + `context_fingerprint` либо client `working_directory`. MCP `roots/list` — workspace hint, а не гарантированный текущий каталог: один `file://` root допустим, несколько требуют уточнения. `global_search=true` явно включает чтение всех текущих проектов без клиентского fingerprint, с общим deadline, двумя активными project calls и глобальными result/graph limits. Неполный ответ имеет `partial: true` и статус `unavailable`.

Дополнительные переменные процесса:

| Переменная | Значение по умолчанию |
| --- | --- |
| `RAG_SERVER_URL` | `http://127.0.0.1:8181` |
| `BIND_ADDRESS` | `127.0.0.1` |
| `RAG_SERVER_PORT` | `8181` |
| `ALLOWED_INDEX_ROOTS` | Пусто; legacy-настройка не создаёт проект. Используйте `projects.json`. |
| `PROJECTS_REGISTRY_FILE` | `~/.config/local-optimize/projects.json` |
| `LOCAL_OPTIMIZE_STATE_DIR` | `~/.local/state/local-optimize`; inference lock и runtime state. |
| `MODEL_PATH`, `TOKENIZER_PATH` | `models/multilingual-e5-small` |
| `RAG_RELEVANCE_FLOOR` | `0.50` |
| `WATCHDOG_RECONCILE_SECONDS` | `300`; минимум `1`, период обновления реестра и metadata reconciliation. |
| `OLLAMA_HOST` | `127.0.0.1:11434`; разрешён только прямой HTTP loopback. |

Это переменные **процесса**, а не дополнительные секреты. При ручном запуске задавайте их через `export`; для systemd используйте собственный override unit-файла. Не помещайте реальные пути и значения в публичные примеры.

## 5. Локальные модели

### E5 и Qwen3 reranker

`huggingface_hub` устанавливается вместе с зависимостями Transformers. Следующая команда скачивает полные snapshots в ожидаемые проектом каталоги:

```bash
python - <<'PY'
from pathlib import Path
from huggingface_hub import snapshot_download

root = Path.cwd()
snapshot_download(
    repo_id="intfloat/multilingual-e5-small",
    revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
    local_dir=root / "models" / "multilingual-e5-small",
)
snapshot_download(
    repo_id="Qwen/Qwen3-Reranker-0.6B",
    revision="e61197ed45024b0ed8a2d74b80b4d909f1255473",
    local_dir=root / "models" / "Qwen3-Reranker-0.6B",
)
PY
```

Проверьте наличие основных файлов:

```bash
test -f models/multilingual-e5-small/config.json
test -f models/multilingual-e5-small/modules.json
test -f models/Qwen3-Reranker-0.6B/config.json
test -f models/Qwen3-Reranker-0.6B/model.safetensors
```

Сервер загружает обе модели с `local_files_only=True`, Qwen — также с `trust_remote_code=False`. Каталог `models/` исключён из Git. Лицензии: E5 — MIT, Qwen3 — Apache-2.0; подробности в `THIRD_PARTY_NOTICES.md`.

### Ollama

```bash
ollama pull qwen2.5-coder:7b
```

`local_model.json` фиксирует имя модели и `num_ctx=8192`. Не задавайте конфликтующие `OLLAMA_MODEL` или `OLLAMA_NUM_CTX`: конфигурация отклонит их. Модель загружается при первом inference, а не при старте HTTP-процесса. Для запроса требуется одна resident-модель с подтверждённым полным GPU-размещением по телеметрии Ollama; CPU fallback нет.

## 6. Миграции

После настройки `secrets.env`:

```bash
python create_schema.py
```

Тот же версионированный runner можно вызвать напрямую:

```bash
python migration_runner.py
```

Обязательные миграции и SHA-256 перечислены в `migrations/manifest.json`. Каждая транзакционная миграция фиксируется вместе со своей registry-записью. Ошибка текущей миграции откатывается, уже зафиксированные версии образуют committed boundary и не откатываются деструктивным SQL. Подробности — `MIGRATIONS.md`.

## 7. Ручной запуск и health-проверки

Запустите сервер из активированного окружения:

```bash
python gpu_rag_server.py
```

В другом терминале, если bearer-токен не настроен:

```bash
curl --fail http://127.0.0.1:8181/liveness
curl --fail http://127.0.0.1:8181/readiness
curl --fail http://127.0.0.1:8181/runtime
```

При настроенном токене добавьте HTTP-заголовок `Authorization: Bearer <token>`. Не печатайте токен в терминальных логах. Ожидаемые ответы:

- `/liveness`: HTTP 200, `{"status":"success"}`; проверяет только процесс.
- `/readiness`: HTTP 200, `{"status":"success","ready":true}` только после проверки engine, PostgreSQL, миграций и минимального запроса; иначе HTTP 503 со статусом `unavailable`.
- `/runtime`: подтверждает размещение моделей на XPU и отключённый CPU fallback.

Для автоматической индексации активного корня в отдельном терминале:

```bash
python watchdog_indexer.py
```

Watchdog обслуживает все зарегистрированные корни одним worker, замечает изменения `projects.json` без restart и исключает вложенные корни других владельцев, модели, зависимости, артефакты, evaluation и symlink-пути. Отключение источника не удаляет знания. Он не заменяет первоначальную полную индексацию. При необходимости выполните её из клиентского каталога или с явным контекстом:

```bash
python /absolute/path/to/local-optimize/index_project_codebase.py
# либо
python /absolute/path/to/local-optimize/index_project_codebase.py \
  --project-id parent-project --root-id primary
```

Без однозначного зарегистрированного контекста команда завершится с ошибкой. Массовая переиндексация не запускается автоматически.

## 8. Local-only MCP

Полный `mcp_server.py` и read-only `lmstudio_rag_mcp.py` используют stdio. Для LM Studio предпочтителен read-only wrapper. `search_local_rag` принимает `working_directory`, explicit `project_id`/`root_id` или `global_search=true`; значения по умолчанию не выбирают проект движка. Пример стандартной MCP-записи (пути замените на собственные):

```json
{
  "mcpServers": {
    "local-rag": {
      "command": "/absolute/path/to/local-optimize/.venv/bin/python",
      "args": [
        "/absolute/path/to/local-optimize/lmstudio_rag_mcp.py"
      ]
    }
  }
}
```

Точное расположение MCP-настройки зависит от версии LM Studio. Не переключайте чат с этим инструментом на облачную модель. Генератор `mcp_config_generator.py` и installer намеренно отклоняют текущие cloud-capable клиенты; не обходите это ограничение вручную.

## 9. Пользовательские systemd-службы

Сначала проверьте план installer:

```bash
python install_central_system.py --dry-run --skip-systemd
```

После успешного ручного запуска можно установить две пользовательские службы:

```bash
python install_central_system.py
```

Installer создаёт резервные копии управляемых файлов, мигрирует только разрешённые секреты, проверяет БД, применяет миграции, записывает units и проверяет liveness/readiness с bearer-заголовком. Без `--client` он не изменяет MCP-конфигурации клиентов. Не используйте `--all-clients` как способ обойти privacy-блокировку.

Проверка:

```bash
systemctl --user status gpu-rag-server.service watchdog-indexer.service
journalctl --user -u gpu-rag-server.service -n 50 --no-pager
journalctl --user -u watchdog-indexer.service -n 50 --no-pager
```

`sudo` не нужен. Installer не откатывает уже committed PostgreSQL-миграции при последующем файловом сбое и явно сообщает эту границу.

## 10. Тесты и диагностика

```bash
python -m compileall -q .
python -m pytest --collect-only -q
python -m pytest -q -m unit
python -m pytest -q -m integration
python -m pytest -q -m postgres
python -m pytest -q -m network
python -m pytest -q -m systemd
python -m ruff check .
```

Fresh-database тесты требуют `CREATEDB` и возможности установить pgvector во временной отдельной БД. При недостатке привилегий они обязаны явно завершиться SKIP, а не переключаться на рабочую схему. Network-тест требует явного `RAG_NETWORK_TEST_URL`.

Типовые проблемы:

- **readiness 503** — проверьте PostgreSQL, миграции, модели и XPU; liveness 200 сам по себе не означает готовность.
- **401** — клиент не отправил bearer-токен из того же защищённого `secrets.env`.
- **`MODEL_POLICY_CONFLICT`** — в Ollama загружена другая/дополнительная модель или изменён контекст.
- **`LOCAL_MODEL_NOT_FULLY_ON_GPU`** — телеметрия не подтверждает полное GPU-размещение; CPU fallback намеренно отсутствует.
- **`LOCAL_MODEL_BUSY` / `RAG_SEARCH_BUSY`** — исчерпаны сознательно ограниченные слоты; повторите запрос после завершения активной работы.
- **MODEL_PATH не найден** — повторите скачивание snapshots и проверьте, что запуск выполняется из корня проекта либо заданы правильные абсолютные пути.

Дополнительный `postgres_backup.sh` требует отдельно выданных прав PostgreSQL и использует `sudo -u postgres`; он не входит в обязательную установку и не должен запускаться без проверки параметров и разрешения администратора.
