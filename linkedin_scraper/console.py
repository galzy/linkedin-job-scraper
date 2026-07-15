"""The one shared Rich console (Rich recommends a single Console per app) and its spinner."""

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

# The log sink (logger.py) and the spinner both render through this, so logs print above the spinner
# instead of corrupting it.
console = Console()


@contextmanager
def live_status(label: str) -> Iterator[None]:
    """Show a single spinning status line, with elapsed time, while the wrapped work runs.

    A no-op without a real terminal (piped or cron), where a spinner can't render.
    """
    if not console.is_terminal:
        yield
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,  # clear the line when done; the summary log takes its place
    ) as progress:
        progress.add_task(label, total=None)
        yield
