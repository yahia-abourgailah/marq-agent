from langchain_openai import ChatOpenAI

from app.config import settings


def get_model() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
    )


__all__ = ["get_model"]