# Сторонние компоненты

Код этого репозитория распространяется по [MIT](LICENSE). Сторонние компоненты сохраняют собственные лицензии; MIT проекта не заменяет их условия.

## Модели

Веса не хранятся в Git. Автономный DEB и его orig tar формируются сборщиком и включают pinned-версии:

- [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) — MIT, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`.
- [Qwen/Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) — Apache-2.0, revision `e61197ed45024b0ed8a2d74b80b4d909f1255473`.
- [qwen2.5-coder:7b](https://ollama.com/library/qwen2.5-coder), manifest SHA-256 `dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364` — условия соответствующего дистрибутива модели Qwen.

При ручной установке для разработки веса скачиваются отдельно по `docs/installation.md`. Перед копированием или повторным распространением весов проверьте полный текст их лицензий и уведомлений в исходных репозиториях.

## Программные зависимости

Python-зависимости перечислены в `requirements.txt`, `requirements-dev.txt` и `deploy/intel-xpu-requirements.txt`. В Git они не копируются. Автономный DEB включает wheel-пакеты и их dist-info/license-файлы в packaged venv; фактические версии зафиксированы в `/usr/lib/local-optimize/requirements.lock` после установки.

Их лицензии находятся в метаданных и документации соответствующих пакетов. Intel XPU runtime и Ollama также сохраняют собственные условия распространения.
