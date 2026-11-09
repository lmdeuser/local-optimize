# Сторонние компоненты

Код этого репозитория распространяется по [MIT](LICENSE). Сторонние компоненты сохраняют собственные лицензии; MIT проекта не заменяет их условия.

## Модели (не входят в репозиторий)

Веса скачиваются отдельно по инструкции из `docs/installation.md`:

- [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) — MIT, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`.
- [Qwen/Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) — Apache-2.0, revision `e61197ed45024b0ed8a2d74b80b4d909f1255473`.
- [qwen2.5-coder:7b](https://ollama.com/library/qwen2.5-coder) загружается через Ollama и регулируется условиями соответствующего дистрибутива модели.

Перед копированием или повторным распространением весов проверьте полный текст их лицензий в исходных репозиториях.

## Программные зависимости

Python-зависимости перечислены в `requirements.txt`, `requirements-dev.txt` и `deploy/intel-xpu-requirements.txt`. Они устанавливаются из соответствующих пакетных репозиториев и не копируются в исходное дерево этого проекта. Их лицензии находятся в метаданных и документации соответствующих пакетов.
