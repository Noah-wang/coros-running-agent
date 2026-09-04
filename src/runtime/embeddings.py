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
    # 嵌入也花钱，也要记账。它没有 completion，只有输入 token。
    # 不记的话「这个月花了多少」会系统性偏低——导入一本书就是几十万 token。
    usage = getattr(response, "usage", None)
    if usage is not None:
        from src.runtime import usage_store

        usage_store.record(
            get_embedding_model(),
            int(getattr(usage, "prompt_tokens", 0) or 0),
            0,
        )

    return [item.embedding for item in response.data]
