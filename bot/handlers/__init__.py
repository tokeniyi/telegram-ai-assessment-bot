from telegram.ext import Application

from . import admin, chat, common, dedupe, explain, guide, student, uploads


def register_handlers(application: Application) -> None:
    dedupe.register(application)  # first: a copy of an update stops before any other handler
    common.register(application)
    admin.register(application)
    explain.register(application)
    uploads.register(application)
    student.register(application)
    chat.register(application)  # its catch-all text handler must not shadow commands
    guide.register(application)  # last: maps the commands above to buttons, answers unknown ones
