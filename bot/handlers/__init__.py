from telegram.ext import Application

from . import admin, chat, common, dedupe, explain, student, uploads


def register_handlers(application: Application) -> None:
    dedupe.register(application)  # first: a copy of an update stops before any other handler
    common.register(application)
    admin.register(application)
    explain.register(application)
    uploads.register(application)
    student.register(application)
    chat.register(application)  # last: its catch-all text handler must not shadow commands
