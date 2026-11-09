"""Regression tests: chunking must never alter indexed text.

The embedding tokenizer (BertTokenizer, do_lower_case=True) is lossy: decoding token
ids lowercases identifiers and inserts spaces around punctuation. Chunks must therefore
be produced by slicing the original string, never by ``tokenizer.decode``.
"""

from pathlib import Path

import pytest
from transformers import AutoTokenizer

import rag_engine
from rag_engine import RagEngine

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def chunker():
    """Bind the chunking methods to a stub carrying only a real tokenizer."""
    tokenizer = AutoTokenizer.from_pretrained(
        rag_engine.TOKENIZER_PATH, local_files_only=True
    )

    class Chunker:
        _token_offsets = RagEngine._token_offsets
        _split_oversized_block = RagEngine._split_oversized_block
        _chunk_text = RagEngine._chunk_text

    instance = Chunker()
    instance.tokenizer = tokenizer
    return instance


CODE_SAMPLE = '''raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

async def verify_api_token(credentials: HTTPAuthorizationCredentials) -> None:
    if credentials.credentials != config.API_TOKEN.get_secret_value():
        raise ValueError("invalid_token")
'''


def test_chunks_are_verbatim_substrings(chunker):
    chunks = chunker._chunk_text(CODE_SAMPLE, max_tokens=400, overlap_tokens=50)
    assert chunks
    for chunk in chunks:
        assert chunk in CODE_SAMPLE


def test_identifier_casing_and_punctuation_survive(chunker):
    joined = "\n".join(chunker._chunk_text(CODE_SAMPLE, max_tokens=400, overlap_tokens=50))
    for probe in (
        "HTTPException",
        "HTTP_401_UNAUTHORIZED",
        "HTTPAuthorizationCredentials",
        "API_TOKEN.get_secret_value()",
        "ValueError",
    ):
        assert probe in joined, f"{probe} was mangled by chunking"
    # The lossy decode path produced artefacts like `http _ 401` and `( status`.
    assert "http _ 401" not in joined
    assert "httpexception" not in joined


def test_oversized_block_is_split_without_rewriting(chunker):
    block = " ".join(f"IdentifierNumber{i}()" for i in range(600))
    chunks = chunker._chunk_text(block, max_tokens=128, overlap_tokens=16)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk in block
    assert "IdentifierNumber599()" in chunks[-1]


def test_token_budget_is_respected(chunker):
    text = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(12))
    max_tokens = 128
    for chunk in chunker._chunk_text(text, max_tokens=max_tokens, overlap_tokens=16):
        count = len(chunker._token_offsets(chunk))
        # Slicing on token boundaries can re-tokenize slightly differently; allow
        # a small margin but reject runaway chunks.
        assert count <= max_tokens + 16


def test_full_source_file_round_trips(chunker):
    source = (Path(__file__).resolve().parents[2] / "project_context.py").read_text()
    chunks = chunker._chunk_text(source, max_tokens=400, overlap_tokens=50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk in source


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n"])
def test_blank_input_yields_no_chunks(chunker, text):
    assert chunker._chunk_text(text) == []


def test_invalid_budget_rejected(chunker):
    with pytest.raises(ValueError):
        chunker._chunk_text("some text", max_tokens=0)
