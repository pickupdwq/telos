"""Prepare a private check HOME from explicitly authorized dependency directories."""
from pathlib import Path
import shutil


def dependency_home(paths, original_home, check_home, *, links=False):
    """Mirror only declared home-relative dependencies, preserving symlinks.

    Check programs sometimes discover a tool under HOME (for example ~/.npm).
    They must see the declared tool without seeing the rest of the real home.
    Symlinks are copied as links, never followed while copying host files; the
    check sandbox still enforces their targets. Returned roots are write-denied.
    """
    selected = []
    roots = set()
    for value in sorted(paths, key=lambda p: (len(Path(p).parts), p)):
        source = Path(value)
        if any(source == parent or parent in source.parents for parent in selected):
            continue
        try:
            relative = source.relative_to(original_home)
        except ValueError:
            continue
        destination = check_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if links:
            if destination.is_symlink() and destination.resolve() == source.resolve():
                pass
            elif destination.exists() or destination.is_symlink():
                raise ValueError('Declared dependency HOME path conflicts with existing workspace data')
            else:
                destination.symlink_to(source, target_is_directory=True)
        else:
            shutil.copytree(source, destination, symlinks=True)
        selected.append(source)
        roots.add(check_home / relative.parts[0])
    return sorted(roots)
