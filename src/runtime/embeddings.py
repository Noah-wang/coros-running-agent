import os

from openai import AsyncOpenAI


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def embedding_configured() -> bool:
    return bool(
        os.getenv("EMBEDDING_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )


def get_embedding_model() -> str:
    return os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def _embedding_client() -> AsyncOpenAI:
    api_key = (
        os.getenv("EMBEDDING_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    if not api_key:
        raise RuntimeError("EMBEDDING_API_KEY is missing. Add it to .env.")

    base_url = (
        os.getenv("EMBEDDING_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
    )
    if base_url:
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    return AsyncOpenAI(api_key=api_key)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    client = _embedding_client()
    response = await client.embeddings.create(
        model=get_embedding_model(),
        input=texts,
    )
    return [item.embedding for item in response.data]
