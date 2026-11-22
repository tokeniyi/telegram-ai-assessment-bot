"""Every command the bot understands, in one place (Stage 6).

The registry is the single source of truth for:

* the command menus (bot/app.py) and the command lists in /help (bot/handlers/common.py);
* the "did you mean" reply to an unknown command (bot/handlers/guide.py);
* every suggestion the bot makes (`Suggestion`): a suggestion naming a command that does not
  exist, or with arguments the command does not accept, is a programming error and raises;
* the commands that buttons may run (`button=True`).

`tests/test_guidance.py` checks the registry against the handlers actually registered with
python-telegram-bot, so a command listed here is one the bot really answers, with the role
restriction stated here.
"""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import BotCommand

if TYPE_CHECKING:
    from .config import Settings

USER, ADMIN = "user", "admin"


@dataclass(frozen=True)
class Arg:
    """One argument. `kind` says how it is checked: "assessment" is an assessment number
    (#3 or 3), "text" is free text (the rest of the message)."""
    name: str
    kind: str
    required: bool = True
    example: str = ""
    # For assessments: the states in which this command can act on one (empty = any).
    states: tuple[str, ...] = ()

    def accepts(self, value: str) -> bool:
        return self.kind == "text" or bool(re.fullmatch(r"#?\d+", value))


@dataclass(frozen=True)
class Command:
    name: str
    summary: str                     # the menu text
    role: str = USER                 # USER: everyone; ADMIN: ADMIN_IDS only
    args: tuple[Arg, ...] = ()
    example: str = ""                # a complete example, e.g. "/close 3"
    # How it can be invoked, besides typing it in a private chat:
    #   "reply"   — as a reply to a message (the message is its input)
    #   "caption" — as the caption of a photo or file
    invocations: tuple[str, ...] = ("command",)
    feature: str | None = None       # a setting that must be on for non-admins (e.g. allow_explain)
    menu: bool = True                # listed in Telegram's command menu
    button: bool = False             # safe to run from a button: read-only, or protected by the database
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        parts = [f"/{self.name}"]
        for arg in self.args:
            parts.append(f"<{arg.name}>" if arg.required else f"[{arg.name}]")
        return " ".join(parts)

    def allowed(self, settings: Settings, user_id: int | None) -> bool:
        if self.role == ADMIN:
            return settings.is_admin(user_id)
        if self.feature and not getattr(settings, self.feature) and not settings.is_admin(user_id):
            return False
        return True


ASSESSMENT = "assessment"
COMMANDS: dict[str, Command] = {c.name: c for c in (
    # ── everyone ──
    Command("start", "Welcome and getting started"),
    Command("pending", "View assessments to complete", button=True),
    Command("myresults", "View your assessment results", button=True),
    Command("cancel", "Discard unsent answers"),
    Command("explain", "Get help with a question or file",
            args=(Arg("question", "text", required=False, example="what is osmosis?"),),
            example="/explain what is osmosis?", invocations=("command", "reply", "caption"),
            feature="allow_explain"),
    Command("help", "How to use the bot", button=True),
    Command("myid", "Show your Telegram ID", menu=False),
    # ── administrators ──
    Command("assessments", "List assessments", role=ADMIN, button=True),
    Command("results", "Scores and CSV export", role=ADMIN, button=True,
            args=(Arg("id", ASSESSMENT, required=False, example="3", states=("published", "closed")),),
            example="/results 3"),
    Command("generate", "Create questions from replied-to notes", role=ADMIN,
            args=(Arg("instructions", "text", required=False, example="8 questions, easy"),),
            example="/generate 8 questions, easy", invocations=("reply",)),
    Command("close", "Stop accepting answers for an assessment", role=ADMIN,
            args=(Arg("id", ASSESSMENT, example="3", states=("published",)),), example="/close 3"),
    # Stage 7. /deadline changes an assessment, so it is typed, never run from a button.
    Command("deadline", "Set when an assessment closes", role=ADMIN,
            args=(Arg("id", ASSESSMENT, example="3", states=("draft", "published")),
                  Arg("when", "text", required=False, example="tomorrow 17:00")),
            example="/deadline 3 tomorrow 17:00"),
    # A button only shows the options; changing one takes a second tap on an explicit value.
    Command("settings", "Deadline, answers shown and resubmission for one assessment", role=ADMIN,
            button=True, args=(Arg("id", ASSESSMENT, example="3"),), example="/settings 3"),
    # A button may run /regrade: each submission is claimed in the database before it is graded,
    # so a double tap or two admins cannot grade one twice.
    Command("regrade", "Retry failed grading", role=ADMIN, button=True,
            args=(Arg("id", ASSESSMENT, example="3", states=("published", "closed")),), example="/regrade 3"),
    Command("users", "Number of registered users", role=ADMIN, button=True),
    Command("status", "Grading, sending and AI provider health", role=ADMIN, button=True),
)}


def get(name: str) -> Command | None:
    name = name.lower().lstrip("/").split("@")[0]
    for command in COMMANDS.values():
        if name == command.name or name in command.aliases:
            return command
    return None


def available(settings: Settings, user_id: int | None) -> list[Command]:
    """The commands this user may use, in registry order."""
    return [c for c in COMMANDS.values() if c.allowed(settings, user_id)]


def closest(name: str, settings: Settings, user_id: int | None) -> list[Command]:
    """Commands this user may use that look like the mistyped `name` (best first)."""
    usable = {c.name: c for c in available(settings, user_id)}
    names = list(usable) + [a for c in usable.values() for a in c.aliases]
    matches = difflib.get_close_matches(name.lower().lstrip("/").split("@")[0], names, n=2, cutoff=0.6)
    return list(dict.fromkeys(get(m) for m in matches))


def menu(admin: bool, settings: Settings) -> list[BotCommand]:
    """Telegram's command menu for everyone, or for an administrator."""
    def shown(c: Command) -> bool:
        if not c.menu or (c.role == ADMIN and not admin):
            return False
        return admin or not c.feature or getattr(settings, c.feature)
    return [BotCommand(c.name, c.summary) for c in COMMANDS.values() if shown(c)]


def code(text: str) -> str:
    """A command (with its arguments) as tap-to-copy code. Telegram's own command links send the
    bare command without its arguments, so commands that take arguments are shown this way."""
    return f"<code>{html.escape(text, quote=False)}</code>"


@dataclass(frozen=True)
class Suggestion:
    """A next step: `/name args…`, why it helps, and optionally a button label.

    Checked against the registry when it is made: the command must exist, every required
    argument must be given, and each argument must have the right form.
    """
    name: str
    args: tuple[str, ...] = ()
    why: str = ""
    button: str | None = None
    command: Command = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        command = get(self.name)
        if command is None:
            raise ValueError(f"suggested /{self.name}, which is not a command")
        if len(self.args) > len(command.args) or len(self.args) < sum(a.required for a in command.args):
            raise ValueError(f"/{self.name} takes {command.usage}, got {self.args}")
        for arg, value in zip(command.args, self.args):
            if not arg.accepts(value):
                raise ValueError(f"/{self.name}: {value!r} is not a valid {arg.name}")
        if self.button and not command.button:
            raise ValueError(f"/{self.name} may not be run from a button")
        object.__setattr__(self, "command", command)

    @property
    def text(self) -> str:
        return " ".join([f"/{self.command.name}", *self.args])

    @property
    def callback_data(self) -> str:
        """`cmd:<name>` or `cmd:<name>:<number>` for the button (Telegram allows 64 bytes)."""
        if len(self.args) > 1 or (self.args and not self.args[0].lstrip("#").isdigit()):
            raise ValueError(f"/{self.text} cannot be put on a button")
        return ":".join(["cmd", self.command.name, *(a.lstrip("#") for a in self.args)])

    def html(self) -> str:
        shown = code(self.text) if self.args else f"/{self.command.name}"
        return f"{self.why}: {shown}" if self.why else shown
