# Local Optimize

Локальная долговременная память и RAG для AI-агентов: PostgreSQL + pgvector, гибридный поиск, GraphRAG, локальный reranking на Intel Arc XPU и изолированная работа с Ollama через MCP.

Проект рассчитан на **local-only** сценарии. Полный RAG MCP нельзя подключать к облачным агентам: найденные фрагменты, их источники и производные данные не должны попадать в облачный диалог. Для локальной LM Studio предусмотрен отдельный read-only wrapper `lmstudio_rag_mcp.py`.

## Возможности

- Гибридный поиск: pgvector, полнотекстовый PostgreSQL, RRF и локальный Qwen3 reranker.
- Изоляция проектов, корней файловой системы, агентов и сессий; проекта по умолчанию нет.
- Автоматический контекст по client cwd/MCP roots и явный bounded global search с project provenance.
- Динамический `projects.json`: независимые и вложенные корни, rootless knowledge, работа без источников.
- Версионированные транзакционные миграции и проверка схемы перед readiness.
- MCP-инструменты поиска, сохранения, проверки и жизненного цикла знаний.
- Локальный Ollama с ограничениями времени, контекста и конкурентных генераций.
- Multi-root watchdog с одним worker, nested ownership и ограниченными очередями.
- Явные `/liveness`, `/readiness` и `/runtime` health-контракты.

Архитектурная схема:

```text
MCP tool → RagClient → FastAPI → RagEngine → PostgreSQL/pgvector
                                      ↓
                         локальные E5 + Qwen3 на Intel XPU
                                      ↓
                         Ollama или безопасный routing signal
```

## Готовый DEB для Ubuntu 26.04

Для Ubuntu 26.04/amd64 используйте пакет из `docs/packaging.md`. Он содержит Python 3.14/XPU-окружение, E5, Qwen reranker и `qwen2.5-coder:7b`; после установки не нужны `pip`, создание venv или скачивание моделей. Секреты и реестр проектов настраиваются отдельно. Runtime устанавливается в системные каталоги корневой файловой системы, которую следует размещать на SSD.

Ниже описана ручная установка для разработки.

## Требования

- Linux с поддерживаемым Intel Arc/XPU-драйвером и OpenCL ICD.
- Python 3.12 или новее; текущий набор проверен на Python 3.14.4. Другие версии не заявляются как проверенные.
- PostgreSQL с расширением pgvector.
- Ollama с моделью `qwen2.5-coder:7b`.
- Достаточное дисковое пространство для PyTorch XPU и отдельно скачиваемых моделей.
- Для служб: пользовательский `systemd`. `sudo` установщику не требуется.

CPU/CUDA/ONNX fallback для production retrieval отсутствует. Без работающего Intel XPU сервер остаётся недоступным по `/readiness` и не выдаёт непроверенный локальный результат.

## Быстрый старт

Полное руководство: **[docs/installation.md](docs/installation.md)**.

```bash
git clone https://github.com/lmdeuser/local-optimize.git
cd local-optimize
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --no-deps -r deploy/intel-xpu-requirements.txt
python -m pip install -r requirements.txt
```

Затем:

1. Создайте БД `aiagent` и включите `vector` по инструкции.
2. Скопируйте `docs/examples/secrets.env.example` в `~/.config/local-optimize/secrets.env`, замените placeholders и установите права `0600`.
3. Скачайте E5 и Qwen3 в `models/` командами из руководства. Веса не хранятся в Git.
4. Выполните `ollama pull qwen2.5-coder:7b`.
5. Примените миграции и запустите сервер:

   ```bash
   python create_schema.py
   python gpu_rag_server.py
   ```

6. В другом терминале при отсутствии bearer-токена проверьте:

   ```bash
   curl --fail http://127.0.0.1:8181/liveness
   curl --fail http://127.0.0.1:8181/readiness
   curl --fail http://127.0.0.1:8181/runtime
   ```

   Если токен настроен, добавьте `Authorization: Bearer <token>`; само значение не записывайте в журнал или репозиторий. Ожидаемый успешный статус liveness/readiness — `success`, readiness также содержит `"ready": true`.

Для фоновой индексации всех зарегистрированных корней запустите `python watchdog_indexer.py`. Без `projects.json` служба не индексирует собственный каталог. Настройка реестра, client cwd/global search, MCP и пользовательских служб описана в руководстве.

## Документация

- [Установка и настройка](docs/installation.md)
- [Сборка DEB и OBS](docs/packaging.md)
- [Архитектура](ARCHITECTURE.md)
- [Полное системное описание](SYSTEM.md)
- [MCP-инструменты](MCP_TOOLS.md)
- [Миграции](MIGRATIONS.md)
- [Схема БД](DATABASE_SCHEMA.md)
- [Тестирование](TESTING.md)
- [Hooks и их privacy-ограничения](docs/hooks.md)
- [Сторонние компоненты и лицензии](THIRD_PARTY_NOTICES.md)

## Проверки

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q .
python -m pytest --collect-only -q
python -m pytest -q -m unit
python -m pytest -q -m integration
python -m pytest -q -m postgres
python -m pytest -q -m network
python -m pytest -q -m systemd
python -m ruff check .
```

Внешние PostgreSQL/network/XPU-проверки могут быть пропущены с явным SKIP при отсутствии их условий. Не направляйте тесты в рабочую БД: fresh-database сценарии создают отдельную временную БД и требуют соответствующих привилегий.

## Безопасность и ограничения

- Секреты находятся только в `~/.config/local-optimize/secrets.env` или переменных процесса; корневой `.env` не является рекомендуемым хранилищем и игнорируется Git.
- Не публикуйте `projects.json`, журналы, дампы, `artifacts/`, модели или локальные IDE/agent-настройки.
- Оценка релевантности не доказывает истинность источника. Проверка ссылок не заменяет независимую проверку утверждений.
- Облачная эскалация через MCP возвращает только routing signal без RAG-текста, источников, оценок и производных данных.
- MCP-генератор намеренно отказывается настраивать текущие cloud-capable клиенты. Используйте отдельное local-only подключение LM Studio.

## Лицензия

Код проекта распространяется по [MIT](LICENSE). Сторонние модели и зависимости используют собственные лицензии — см. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
