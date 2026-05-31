"""
Web module for OptionBuddy multi-tenant dashboard.

Provides FastAPI application for:
- User registration and authentication
- Broker credential management
- Telegram bot linking
- User dashboard and settings
"""

from .main import app

__all__ = ['app']
