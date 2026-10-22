from fastapi import APIRouter

from app.api.routes import chat, health, root

api_router = APIRouter()
api_router.include_router(root.router)
api_router.include_router(health.router)
api_router.include_router(chat.router)
