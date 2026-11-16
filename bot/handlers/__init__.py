from telegram.ext import Application

from . import admin, common, explain, student, uploads


def register_handlers(application: Application) -> None:
    common.register(application)
    admin.register(application)
    explain.register(application)
    uploads.register(application)
    student.register(application)  # last: its catch-all text handler must not shadow commands
