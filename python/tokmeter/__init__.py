"""Local usage accounting; no network or persistent usage cache."""

__version__ = "0.2.1"


def main():
    from .cli import main as run

    return run()
