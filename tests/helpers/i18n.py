from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

from src.core.i18n import DOMAIN
from src.core.locales import SUPPORTED_LOCALES


def compile_catalogs(source_dir: Path, target_dir: Path) -> None:
    """Compile every supported catalog under `source_dir` into `target_dir`.

    The suite loads catalogs compiled from the working tree into a temporary
    directory, so it never depends on - or writes - .mo files in locales/.
    """
    for locale in SUPPORTED_LOCALES:
        with (source_dir / locale / "LC_MESSAGES" / f"{DOMAIN}.po").open("rb") as po:
            catalog = read_po(po, locale=locale)
        target = target_dir / locale / "LC_MESSAGES"
        target.mkdir(parents=True, exist_ok=True)
        with (target / f"{DOMAIN}.mo").open("wb") as mo:
            write_mo(mo, catalog)
