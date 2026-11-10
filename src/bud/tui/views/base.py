"""Component base class for bud TUI views with line caching."""

from __future__ import annotations

from abc import ABC, abstractmethod


class View(ABC):
    """Base class for all TUI views.

    Implements automatic caching of rendered lines for a given width until marked dirty.
    """

    def __init__(self) -> None:
        self._dirty: bool = True
        self._cached_width: int | None = None
        self._cached_lines: list[str] = []

    def mark_dirty(self) -> None:
        """Invalidate the cached rendered lines."""
        self._dirty = True

    def render(self, width: int) -> list[str]:
        """Render the view at width, returning cached lines if unchanged."""
        if not self._dirty and self._cached_width == width:
            return self._cached_lines

        lines = self._render(width)
        self._cached_width = width
        self._cached_lines = lines
        self._dirty = False
        return lines

    @abstractmethod
    def _render(self, width: int) -> list[str]:
        """Abstract render implementation returning a list of terminal string lines."""
        raise NotImplementedError
