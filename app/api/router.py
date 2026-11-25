from fastapi import APIRouter

from app.api.routes import assistant, chat, health, retrieval, root, routing

api_router = APIRouter()
api_router.include_router(root.router)
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(assistant.router)
api_router.include_router(retrieval.router)
api_router.include_router(routing.router)
