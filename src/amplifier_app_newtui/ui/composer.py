"""The composer: mode badge + prompt glyph + auto-height input
(DESIGN-SPEC §2 item 5, §5).

Single-line feel via a TextArea that grows with content (capped). The
left edge is tinted 2px in the mode accent — chat uses the ``rule``
token (spec §4). The ``[mode]`` badge is clickable (cycles the mode) and
the ``❯`` prompt is green bold.

Input semantics are POSTED AS MESSAGES; the composer never executes
anything itself:

- Enter        → :class:`Composer.Steer` while ``running`` else
                 :class:`Composer.Submit` (the app owns the running flag
                 and sets it on the composer — steer-vs-submit is the
                 app's call, made through that flag).
- Shift+Enter  → :class:`Composer.QueueMessage` (alt+enter is the
                 always-registered legacy-terminal fallback; the
                 ``kitty_protocol`` probe flag only changes which chord
                 is *advertised*).
- Esc          → :class:`Composer.EscPressed` (app resolves via
                 ``keymap.ESC_CHAIN``).
- ``/`` prefix → :class:`Composer.OpenPalette` with the live filter,
                 re-posted on every edit while the text keeps the ``/``
                 prefix; :class:`Composer.PaletteFilterCleared` when the
                 prefix is deleted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from time import monotonic

from textual import events
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static, TextArea

from ..kernel.clipboard import ImageAttachment, pasted_image_attachments
from ..model.modes import DEFAULT_MODE, ModeProfile, get_mode
from .file_mentions import FileMentionIntent
from .keymap import COMPOSER_PLACEHOLDER, hint_label

MAX_INPUT_HEIGHT = 6
"""Cap on the auto-growing input, in lines."""

MAX_PROMPT_HISTORY = 500
"""Bound the in-memory prompt ring without truncating individual prompts."""

PASTE_LINE_THRESHOLD = 10
PASTE_CHAR_THRESHOLD = 800
"""A paste larger than either collapses to a stub (amplifier-app-cli
``LosslessTextPasteState`` parity): the composer shows a compact
``[Pasted #N · … ]`` placeholder while the full text is retained and
expanded verbatim at submit — so a big paste never floods the composer
(what read as 'truncated') and nothing is lost."""

PASTE_DUPLICATE_WINDOW_SECONDS = 0.15
"""Ignore an identical terminal paste replayed immediately.

Some terminal/input stacks occasionally deliver the same bracketed-paste
sequence twice.  The fence is deliberately narrow and also requires the
composer text and cursor to be unchanged since the first insertion, so a
later intentional repeat or any intervening edit still works normally.
"""

_MODE_CLASSES = ("mode-chat", "mode-plan", "mode-brainstorm", "mode-build", "mode-auto")
_FILE_MENTION_RE = re.compile(r"(?<!\S)@([^\s@]*)$")


def _cursor_offset(text: str, location: tuple[int, int]) -> int:
    """Translate TextArea's ``(row, column)`` cursor into a text offset."""
    row, column = location
    lines = text.splitlines(keepends=True)
    return sum(len(line) for line in lines[:row]) + column


def _cursor_location(text: str, offset: int) -> tuple[int, int]:
    """Translate a text offset back into TextArea's cursor location."""
    prefix = text[:offset]
    return (prefix.count("\n"), len(prefix.rsplit("\n", 1)[-1]))


def active_file_mention(
    text: str, location: tuple[int, int]
) -> tuple[str, int, int] | None:
    """Return ``(query, start, end)`` for the mention under the cursor."""
    end = _cursor_offset(text, location)
    match = _FILE_MENTION_RE.search(text[:end])
    if match is None:
        return None
    return (match.group(1), match.start(), end)


class ModeBadge(Static):
    """The clickable ``[mode]`` badge; clicking requests a mode cycle."""

    DEFAULT_CSS = """
    ModeBadge {
        width: auto;
        height: 1;
        padding: 0 1 0 0;
    }
    ModeBadge.mode-chat { color: $dim; }
    ModeBadge.mode-plan { color: $blue; }
    ModeBadge.mode-brainstorm { color: $teal; }
    ModeBadge.mode-build { color: $green; }
    ModeBadge.mode-auto { color: $orange; }
    """

    def __init__(self) -> None:
        # markup=False: the literal text "[chat]" must never parse as markup.
        super().__init__("", markup=False)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(Composer.CycleModeRequested())


class ComposerInput(TextArea):
    """The text input: auto-height, spec placeholder, key semantics.

    Key handling is intercepted BEFORE TextArea's own editing bindings so
    Enter never inserts a newline; everything else falls through to the
    stock TextArea behavior.
    """

    DEFAULT_CSS = """
    ComposerInput {
        width: 1fr;
        height: auto;
        max-height: 6;
        border: none;
        padding: 0;
        background: transparent;
    }
    ComposerInput:focus { border: none; }
    ComposerInput .text-area--placeholder { color: $dimmer; }
    """

    def __init__(self) -> None:
        super().__init__(placeholder=COMPOSER_PLACEHOLDER, soft_wrap=True)
        self._last_paste: tuple[str, str, tuple[int, int], float] | None = None

    def _is_duplicate_paste(self, payload: str) -> bool:
        """True only for an unchanged, immediate replay of *payload*."""

        stamp = self._last_paste
        if stamp is None:
            return False
        previous_payload, result_text, result_cursor, accepted_at = stamp
        return (
            payload == previous_payload
            and monotonic() - accepted_at <= PASTE_DUPLICATE_WINDOW_SECONDS
            and self.text == result_text
            and self.cursor_location == result_cursor
        )

    def _remember_paste(self, payload: str) -> None:
        self._last_paste = (payload, self.text, self.cursor_location, monotonic())

    async def _on_key(self, event: events.Key) -> None:
        composer = self._composer()
        if composer is None:
            await super()._on_key(event)
            return
        if composer.mention_open and event.key in ("up", "down"):
            event.stop()
            event.prevent_default()
            composer.post_message(
                FileMentionIntent("move", delta=-1 if event.key == "up" else 1)
            )
        elif composer.mention_open and event.key in ("enter", "tab"):
            event.stop()
            event.prevent_default()
            composer.post_message(FileMentionIntent("accept"))
        elif composer.mention_open and event.key == "escape":
            event.stop()
            event.prevent_default()
            composer.post_message(FileMentionIntent("clear"))
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            composer.handle_enter()
        elif event.key in ("shift+enter", "alt+enter"):
            event.stop()
            event.prevent_default()
            composer.handle_queue()
        elif event.key in ("ctrl+j", "ctrl+enter"):
            # Multi-line input, amplifier-app-cli parity (its banner:
            # "Multi-line: Ctrl-J"). Ctrl+Enter is a terminal-supported
            # alternate; the TextArea grows to max-height 6.
            # Ignored while empty: automation that sends Enter as CRLF
            # (e.g. node-pty key helpers) must not leave a phantom
            # newline in the just-cleared composer.
            event.stop()
            event.prevent_default()
            if self.text:
                composer.end_history_navigation()
                self.insert("\n")
        elif event.key == "up":
            # Shell-style prompt history wins for a single-line draft (or
            # while already browsing). Multi-line drafts retain TextArea's
            # native vertical cursor movement.
            history_eligible = composer.history_browsing or "\n" not in self.text
            if history_eligible and composer.history_previous():
                event.stop()
                event.prevent_default()
            elif not self.text:
                # With no history, preserve lanes-panel navigation.
                event.stop()
                event.prevent_default()
                composer.post_message(Composer.NavKey(-1))
            else:
                await super()._on_key(event)
        elif event.key == "down":
            if composer.history_next():
                event.stop()
                event.prevent_default()
            elif not self.text:
                event.stop()
                event.prevent_default()
                composer.post_message(Composer.NavKey(1))
            else:
                await super()._on_key(event)
        elif event.key == "ctrl+v":
            # Clipboard image paste (amplifier-app-cli parity): the app
            # reads the system clipboard off-thread; text paste stays on
            # the terminal's bracketed-paste path (_on_paste).
            event.stop()
            event.prevent_default()
            composer.post_message(Composer.PasteImage())
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            composer.post_message(Composer.EscPressed())
        else:
            composer.end_history_navigation()
            await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        # Own the paste so a big block collapses to a stub instead of
        # flooding the composer (amplifier-app-cli parity). Small pastes
        # fall through to TextArea's verbatim insert.
        composer = self._composer()
        if composer is None or not event.text:
            await super()._on_paste(event)
            return
        if self._is_duplicate_paste(event.text):
            event.stop()
            event.prevent_default()
            return
        composer.end_history_navigation()
        # Cmd+V of an image file and drag-and-drop both arrive here as a
        # bracketed paste of the file path — attach them, don't insert text.
        images = pasted_image_attachments(event.text)
        if images:
            event.stop()
            event.prevent_default()
            for image in images:
                composer.add_image(image)
            self._remember_paste(event.text)
            return
        stub = composer.register_paste(event.text)
        if stub is None:
            # Paste bubbles in Textual. We invoke TextArea's insertion
            # explicitly, so stop the original event here; otherwise the same
            # event is re-dispatched while it climbs the composer/app tree and
            # the payload is inserted repeatedly.
            event.stop()
            event.prevent_default()
            await super()._on_paste(event)
            self._remember_paste(event.text)
            return
        event.stop()
        event.prevent_default()
        self.insert(stub)
        self._remember_paste(event.text)

    def _composer(self) -> "Composer | None":
        node = self.parent
        while node is not None:
            if isinstance(node, Composer):
                return node
            node = node.parent
        return None


class Composer(Horizontal):
    """[mode] ❯ <input> — the bottom input strip."""

    DEFAULT_CSS = """
    Composer {
        width: 100%;
        height: auto;
        background: $bg-chrome;
        padding: 0 1;
    }
    Composer.mode-chat { border-left: thick $rule; }
    Composer.mode-plan { border-left: thick $blue; }
    Composer.mode-brainstorm { border-left: thick $teal; }
    Composer.mode-build { border-left: thick $green; }
    Composer.mode-auto { border-left: thick $orange; }
    Composer > .composer-prompt {
        width: auto;
        height: 1;
        color: $green;
        text-style: bold;
        padding: 0 1 0 0;
    }
    """

    # -- messages ------------------------------------------------------------

    class Submit(Message):
        """Idle Enter: send *text* as a new user turn, with any staged
        clipboard images whose ``[Image #N]`` token survives in *text*."""

        def __init__(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> None:
            self.text = text
            self.attachments = attachments
            super().__init__()

    class PasteImage(Message):
        """Ctrl+V: the app reads the system clipboard image off-thread."""

    class Steer(Message):
        """Running Enter: steer the current turn with *text*."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class QueueMessage(Message):
        """Shift+Enter (or alt+enter): queue *text* as the full next turn."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class OpenPalette(Message):
        """Composer text starts with ``/`` — open/refilter the palette."""

        def __init__(self, filter: str) -> None:
            self.filter = filter
            super().__init__()

    class PaletteFilterCleared(Message):
        """The ``/`` prefix was deleted — the palette filter is gone."""

    class EscPressed(Message):
        """Esc in the composer; the app resolves it via ``ESC_CHAIN``."""

    class NavKey(Message):
        """↑/↓ on an EMPTY composer — the app routes it to an open,
        unfocused overlay strip (auto-opened lanes panel, spec §8)."""

        def __init__(self, delta: int) -> None:
            self.delta = delta
            super().__init__()

    class EnterEmpty(Message):
        """Enter on an EMPTY composer — focus the selected lane when the
        lanes panel is open (otherwise ignored, as before)."""

    class CycleModeRequested(Message):
        """The ``[mode]`` badge was clicked; the app cycles the mode."""

    # -- lifecycle -------------------------------------------------------------

    def __init__(
        self,
        *,
        kitty_protocol: bool = True,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.kitty_protocol = kitty_protocol
        self.running: bool = False
        self._mode: ModeProfile = get_mode(DEFAULT_MODE)
        self._palette_open = False
        self._mention_filter_active = False
        self.mention_open = False
        self._badge = ModeBadge()
        self._prompt = Static("❯", classes="composer-prompt")
        self._input = ComposerInput()
        self._pastes: dict[str, str] = {}  # stub → full retained payload
        self._paste_seq = 0
        self._attachments: list[tuple[str, ImageAttachment]] = []  # (placeholder, image)
        self._image_seq = 0
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""

    def compose(self):
        yield self._badge
        yield self._prompt
        yield self._input

    def on_mount(self) -> None:
        self._apply_mode()

    # -- public API --------------------------------------------------------------

    @property
    def mode(self) -> ModeProfile:
        return self._mode

    def set_mode(self, profile: ModeProfile) -> None:
        """Adopt *profile*: badge text/color and left-edge accent update."""
        self._mode = profile
        self._apply_mode()

    @property
    def text(self) -> str:
        return self._input.text

    @property
    def selected_text(self) -> str:
        """The input's own selection (the ctrl+c copy source of truth)."""
        return self._input.selected_text

    def clear(self) -> None:
        self._input.clear()
        self.end_history_navigation()
        self.mention_open = False
        self._pastes.clear()
        self._attachments.clear()
        self._image_seq = 0

    def add_image(self, attachment: ImageAttachment) -> None:
        """Stage a clipboard image and insert its ``[Image #N]`` placeholder
        (deleting the placeholder before submit drops the image)."""
        self._image_seq += 1
        self.end_history_navigation()
        placeholder = f"[Image #{self._image_seq}]"
        self._attachments.append((placeholder, attachment))
        prefix = "" if not self._input.text or self._input.text.endswith((" ", "\n")) else " "
        self._input.insert(f"{prefix}{placeholder} ")

    def _staged_attachments(self, text: str) -> tuple[ImageAttachment, ...]:
        """Images whose placeholder survives in *text* (spec: a deleted
        ``[Image #N]`` token drops that attachment)."""
        return tuple(image for placeholder, image in self._attachments if placeholder in text)

    def register_paste(self, text: str) -> str | None:
        """Retain a long paste and return its stub; ``None`` to insert
        *text* inline (short pastes stay verbatim in the composer)."""
        line_count = text.count("\n") + 1
        if line_count <= PASTE_LINE_THRESHOLD and len(text) <= PASTE_CHAR_THRESHOLD:
            return None
        self._paste_seq += 1
        measure = (
            f"{line_count} lines" if line_count > PASTE_LINE_THRESHOLD else f"{len(text)} chars"
        )
        stub = f"[Pasted #{self._paste_seq} · {measure}]"
        self._pastes[stub] = text
        return stub

    def _expand(self, text: str) -> str:
        """Replace retained paste stubs with their full payloads."""
        for stub, payload in self._pastes.items():
            text = text.replace(stub, payload)
        return text

    def insert_text(self, text: str) -> None:
        """Insert *text* at the cursor (key pass-through from overlay
        strips — e.g. typing while the lanes panel holds focus)."""
        self.end_history_navigation()
        self._input.insert(text)

    def seed_history(self, prompts: Iterable[str]) -> None:
        """Load persisted user prompts so resumed sessions keep ↑ history."""
        for prompt in prompts:
            self._remember_prompt(prompt)
        self.end_history_navigation()

    @property
    def history_browsing(self) -> bool:
        return self._history_index is not None

    def history_previous(self) -> bool:
        """Recall the previous prompt, preserving the current draft."""
        if not self._history:
            return False
        if self._history_index is None:
            self._history_draft = self._input.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._load_history_text(self._history[self._history_index])
        return True

    def history_next(self) -> bool:
        """Move toward newer prompts and finally restore the saved draft."""
        if self._history_index is None:
            return False
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._load_history_text(self._history[self._history_index])
        else:
            draft = self._history_draft
            self.end_history_navigation()
            self._load_history_text(draft)
        return True

    def end_history_navigation(self) -> None:
        self._history_index = None
        self._history_draft = ""

    def apply_file_mention(self, path: str) -> bool:
        """Replace the active ``@query`` with *path* and keep typing."""
        self.end_history_navigation()
        active = active_file_mention(self._input.text, self._input.cursor_location)
        if active is None:
            return False
        _, start, end = active
        rendered = f'@"{path}"' if any(char.isspace() for char in path) else f"@{path}"
        text = self._input.text
        replacement = f"{rendered} "
        updated = f"{text[:start]}{replacement}{text[end:]}"
        cursor = start + len(replacement)
        self._input.load_text(updated)
        self._input.cursor_location = _cursor_location(updated, cursor)
        self.mention_open = False
        self._mention_filter_active = False
        self.post_message(FileMentionIntent("clear"))
        return True

    def focus_input(self) -> None:
        self._input.focus()

    @property
    def queue_hint(self) -> str:
        """The advertised queue chord: shift+enter, or alt+enter when the
        kitty keyboard protocol is absent (terminal probe flag)."""
        overrides = None if self.kitty_protocol else {"queue_message": "alt+enter"}
        return hint_label("queue_message", overrides)

    # -- input semantics -----------------------------------------------------------

    def handle_enter(self) -> None:
        # Stubs are expanded to their full payloads for submission while
        # the composer only ever showed the compact placeholder.
        raw = self._input.text
        text = self._expand(raw).strip()
        if not text:
            self.post_message(self.EnterEmpty())
            return
        self._remember_prompt(text)
        if self.running:
            # Steering is text-only (images ride a fresh submit only).
            self.post_message(self.Steer(text))
        else:
            self.post_message(self.Submit(text, self._staged_attachments(raw)))
        self.clear()

    def handle_queue(self) -> None:
        text = self._expand(self._input.text).strip()
        if not text:
            return
        self._remember_prompt(text)
        self.post_message(self.QueueMessage(text))
        self.clear()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        text = self._input.text
        if text.startswith("/"):
            self._palette_open = True
            # Mockup onInput: the live filter is the TRIMMED value, so
            # "/mode " (trailing space) still matches /mode.
            self.post_message(self.OpenPalette(filter=text.strip()))
            if self._mention_filter_active:
                self._mention_filter_active = False
                self.post_message(FileMentionIntent("clear"))
            return
        if self._palette_open:
            self._palette_open = False
            self.post_message(self.PaletteFilterCleared())
        mention = active_file_mention(text, self._input.cursor_location)
        if mention is not None:
            self._mention_filter_active = True
            self.post_message(FileMentionIntent("filter", query=mention[0]))
        elif self._mention_filter_active:
            self._mention_filter_active = False
            self.mention_open = False
            self.post_message(FileMentionIntent("clear"))

    # -- internals ---------------------------------------------------------------

    def _remember_prompt(self, text: str) -> None:
        prompt = text.strip()
        if not prompt or (self._history and self._history[-1] == prompt):
            return
        self._history.append(prompt)
        if len(self._history) > MAX_PROMPT_HISTORY:
            del self._history[: len(self._history) - MAX_PROMPT_HISTORY]

    def _load_history_text(self, text: str) -> None:
        self._input.load_text(text)
        self._input.cursor_location = _cursor_location(text, len(text))

    def _apply_mode(self) -> None:
        mode_class = f"mode-{self._mode.id}"
        for cls in _MODE_CLASSES:
            self.set_class(cls == mode_class, cls)
            self._badge.set_class(cls == mode_class, cls)
        self._badge.update(f"[{self._mode.id}]")


__all__ = [
    "Composer",
    "ComposerInput",
    "MAX_INPUT_HEIGHT",
    "ModeBadge",
    "active_file_mention",
]
