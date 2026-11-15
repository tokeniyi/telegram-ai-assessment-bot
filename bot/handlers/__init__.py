from telegram.ext import Application

from . import admin, common, student


def register_handlers(application: Application) -> None:
    common.register(application)
    admin.register(application)
    student.register(application)  # last: its catch-all text handler must not shadow commands
