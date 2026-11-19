"""Check active Markdown style and local links without fetching remote resources.

This deliberately recognizes ordinary Markdown links and headings, not all of CommonMark.
Historical records and copied model notices are preserved evidence and excluded.
Sentence checking is conservative: code, tables, headings, quotes and abbreviations are ignored.
"""

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = ("docs/history/", "worker/notices/")
INLINE_CODE = re.compile(r"(`+).*?\1")
INLINE_LINK = re.compile(
    r"!?\[[^\]\n]*\]\((<[^>\n]+>|(?:[^()\s]|\([^()\n]*\))+)"
    r"""(?:\s+(?:"[^"]*"|'[^']*'))?\)"""
)
REFERENCE = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)")
REFERENCE_LINK = re.compile(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]")


def prose_lines(text):
    """Yield source line numbers outside fenced code and HTML comments."""
    fence = None
    comment = False
    for number, line in enumerate(text.splitlines(), 1):
        if comment:
            if "-->" not in line:
                continue
            line = line.split("-->", 1)[1]
            comment = False
        line = re.sub(r"<!--.*?-->", "", line)
        if "<!--" in line:
            line = line.split("<!--", 1)[0]
            comment = True
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if fence is None and not line.startswith(("    ", "\t")):
            yield number, line


def heading_anchors(text):
    anchors = set()
    lines = list(prose_lines(text))
    for index, (_, line) in enumerate(lines):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        title = heading[1] if heading else None
        if index and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", line):
            title = lines[index - 1][1].strip()
        if title:
            title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", title)
            title = re.sub(r"<[^>]+>", "", title)
            base = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            slug, suffix = base, 0
            while slug in anchors:
                suffix += 1
                slug = f"{base}-{suffix}"
            anchors.add(slug)
        anchors.update(re.findall(r"""\b(?:id|name)=["']([^"']+)["']""", line))
    return anchors


def multiple_sentences(line):
    if re.match(r"^\s*(?:#{1,6}\s|\||<|={3,}\s*$|-{3,}\s*$)", line):
        return False
    if line.startswith(("    ", "\t")) or REFERENCE.match(line):
        return False
    line = re.sub(r"^\s*(?:>\s*)?(?:[-*+]|\d+[.)])\s+", "", line)
    line = INLINE_CODE.sub("", line)
    line = INLINE_LINK.sub("", line)
    line = re.sub(r'"[^"]*"|“[^”]*”', "", line)
    line = re.sub(r"https?://\S+", "", line)
    line = re.sub(r"\b(?:e\.g|i\.e|etc|vs|Mr|Mrs|Ms|Dr|Prof)\.", "", line)
    return bool(re.search(r'[.!?][)"\']?\s+[A-Z][a-z]{2,}\b', line))


def check_document(path, root=ROOT):
    errors = []
    text = path.read_text()
    lines = list(prose_lines(text))
    definitions = {
        match[1].casefold(): match[2] for _, line in lines if (match := REFERENCE.match(line))
    }
    for number, line in enumerate(text.splitlines(), 1):
        if "\u2014" in line:
            errors.append(f"{path}:{number}: use a plain hyphen instead of an em dash")
    for number, line in lines:
        if multiple_sentences(line):
            errors.append(f"{path}:{number}: put each prose sentence on its own line")
        line = INLINE_CODE.sub("", line)
        targets = [match[1] for match in INLINE_LINK.finditer(line)]
        if definition := REFERENCE.match(line):
            targets.append(definition[2])
        for match in REFERENCE_LINK.finditer(line):
            label = (match[2] or match[1]).casefold()
            if label not in definitions:
                errors.append(f"{path}:{number}: undefined link reference [{label}]")
        for target in targets:
            target = target.removeprefix("<").removesuffix(">")
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            location = unquote(url.path)
            destination = (
                root / location.lstrip("/") if location.startswith("/") else path.parent / location
            )
            if not location:
                destination = path
            if not destination.exists():
                errors.append(f"{path}:{number}: missing local link target: {target}")
            elif (
                url.fragment
                and destination.suffix.lower() == ".md"
                and unquote(url.fragment) not in heading_anchors(destination.read_text())
            ):
                errors.append(f"{path}:{number}: missing Markdown anchor: {target}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files", nargs="*", type=Path, help="defaults to active repository Markdown"
    )
    args = parser.parse_args()
    paths = args.files
    if not paths:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.md"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        names = set(result.stdout.decode().split("\0")) - {""}
        paths = [
            ROOT / name
            for name in sorted(names)
            if not name.startswith(EXCLUDED) and (ROOT / name).is_file()
        ]
    errors = [error for path in paths for error in check_document(path.resolve())]
    for error in errors:
        print(error)
    print(f"Checked {len(paths)} Markdown files; {len(errors)} issue(s).")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
