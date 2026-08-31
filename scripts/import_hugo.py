#!/usr/bin/env python3
"""One-shot converter: Hugo blog (D:\\Prasad\\blog) -> Obsidian markdown notes.

Real posts:      content/post/*.md   -> posts/<slug>.md  (publish from draft flag)
Orphaned drafts:  3 known .org files  -> drafts/<slug>.md (publish: false)
Images:           static/images/post/* -> attachments/ (only ones actually referenced)

Usage:
    python scripts/import_hugo.py --out-root D:\\Sandbox\\obsedionblog\\.import-preview --limit 5
    python scripts/import_hugo.py --out-root D:\\Sandbox\\obsedionblog
"""

import argparse
import datetime
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

SOURCE_ROOT = Path(r"D:\Prasad\blog")
POST_DIR = SOURCE_ROOT / "content" / "post"
IMAGES_DIR = SOURCE_ROOT / "static" / "images" / "post"

ORPHAN_ORG_FILES = ["Hugoblog.org", "SST_Nucleo_F303.org", "neovim.org"]

DATE_DISAGREEMENT_DAYS = 60


# ---------- shared text helpers ----------

def sanitize_stem(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "-", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")[:120] or "untitled"


def slugify_filename(stem: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", stem)
    s = s.replace("_", "-")
    s = re.sub(r"[^A-Za-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-").lower() or "untitled"


def normalize_tag(tag: str) -> str:
    tag = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", tag)
    tag = tag.replace("_", "-")
    tag = re.sub(r"-+", "-", tag)
    return tag.strip("-").lower()


def unique_stem(base: str, used: dict) -> str:
    if base not in used:
        used[base] = 1
        return base
    used[base] += 1
    return f"{base}-{used[base]}"


# ---------- frontmatter parsing (TOML '+++' or simple YAML '---') ----------

LIST_RE = re.compile(r"^(\w+):\s*\[(.*)\]\s*$")
QSTR_RE = re.compile(r'^(\w+):\s*"(.*)"\s*$')
BOOL_RE = re.compile(r"^(\w+):\s*(true|false)\s*$", re.IGNORECASE)
BARE_RE = re.compile(r"^(\w+):\s*(.*)$")


MULTILINE_LIST_RE = re.compile(r"^(\w+):\s*\[\s*\n(.*?)\n\s*\]\s*$", re.DOTALL | re.MULTILINE)


def collapse_multiline_lists(block: str) -> str:
    def repl(m):
        items = ", ".join(l.strip() for l in m.group(2).split("\n") if l.strip())
        return f"{m.group(1)}: [{items}]"

    return MULTILINE_LIST_RE.sub(repl, block)


def parse_simple_yaml(block: str) -> dict:
    block = collapse_multiline_lists(block)
    data = {}
    for line in block.split("\n"):
        line = line.rstrip()
        if not line or line.strip() in ("...", "---"):
            continue
        m = LIST_RE.match(line)
        if m:
            items = [i.strip().strip('"').strip("'") for i in m.group(2).split(",")]
            data[m.group(1)] = [i for i in items if i]
            continue
        m = QSTR_RE.match(line)
        if m:
            data[m.group(1)] = m.group(2)
            continue
        m = BOOL_RE.match(line)
        if m:
            data[m.group(1)] = m.group(2).lower() == "true"
            continue
        m = BARE_RE.match(line)
        if m:
            data[m.group(1)] = m.group(2).strip()
    return data


def parse_frontmatter(text: str):
    text = text.replace("\r\n", "\n")
    if text.startswith("+++"):
        end = text.find("\n+++", 3)
        if end == -1:
            return {}, text
        block = text[3:end].strip("\n")
        body = text[end + 4:]
        try:
            data = tomllib.loads(block)
        except Exception:
            data = {}
        return data, body.lstrip("\n")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end == -1:
            return {}, text
        block = text[3:end].strip("\n")
        body = text[end + 4:]
        return parse_simple_yaml(block), body.lstrip("\n")
    return {}, text


def stringify_date(value) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.date().isoformat() if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, str) and value:
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            m = re.match(r"^\d{4}-\d{2}-\d{2}", value)
            if m:
                return m.group(0)
    return None


def git_first_commit_date(relpath: str):
    # No --follow: with many near-identical empty archetype stub files, git's
    # rename-detection heuristic falsely matches unrelated files' history together.
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%ai", "--", relpath],
            cwd=SOURCE_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return None
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return None
    return lines[-1][:10]  # oldest event, YYYY-MM-DD


def resolve_date(frontmatter_date_str, relpath, report):
    git_date = git_first_commit_date(relpath)
    if not frontmatter_date_str:
        if git_date:
            report["date_from_git_only"].append(relpath)
            return git_date
        return None
    if not git_date:
        return frontmatter_date_str
    fm = datetime.date.fromisoformat(frontmatter_date_str)
    gd = datetime.date.fromisoformat(git_date)
    if abs((fm - gd).days) > DATE_DISAGREEMENT_DAYS:
        report["date_overridden_by_git"].append((relpath, frontmatter_date_str, git_date))
        return git_date
    return frontmatter_date_str


# ---------- image handling ----------

def resolve_image(name: str):
    candidate = IMAGES_DIR / name
    if candidate.exists():
        return candidate
    lower = name.lower()
    for f in IMAGES_DIR.iterdir():
        if f.name.lower() == lower:
            return f
    return None


def copy_image(name: str, post_stem: str, attachments_dir: Path, report):
    src = resolve_image(name)
    if src is None:
        report["missing_images"].append(name)
        return None
    new_name = sanitize_stem(f"{post_stem}-{src.stem}") + src.suffix.lower()
    dest = attachments_dir / new_name
    if not dest.exists():
        attachments_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        report["images_copied"] += 1
    return new_name


# ---------- Hugo markdown body conversion ----------

FIGURE_RE = re.compile(r"\{\{<\s*figure\s+([^>]*?)\s*/?>\}\}")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(/images/post/([^)\s]+)\)")
HIGHLIGHT_RE = re.compile(
    r"\{\{<\s*highlight\s+(\S+)[^>]*>\}\}\n(.*?)\{\{<\s*/\s*highlight\s*>\}\}\n?",
    re.DOTALL | re.IGNORECASE,
)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
MORE_RE = re.compile(r"^<!--more-->\n?", re.MULTILINE)
HEADING_NO_SPACE_RE = re.compile(r"^(#{1,6})(?!#)(\S)", re.MULTILINE)
HEADING_ANCHOR_RE = re.compile(r"[ \t]*\{#[a-z0-9-]+\}[ \t]*$", re.MULTILINE)


def convert_hugo_body(text: str, post_stem: str, attachments_dir: Path, report) -> str:
    text = MORE_RE.sub("", text)

    def repl_highlight(m):
        lang, body = m.group(1), m.group(2)
        return f"```{lang}\n{body}```\n"

    text = HIGHLIGHT_RE.sub(repl_highlight, text)

    def repl_figure(m):
        attrs = dict(ATTR_RE.findall(m.group(1)))
        src = attrs.get("src", "")
        basename = src.rsplit("/", 1)[-1]
        new_name = copy_image(basename, post_stem, attachments_dir, report)
        if new_name is None:
            return f"<!-- broken image link: {src} -->"
        embed = f"![[attachments/{new_name}]]"
        if attrs.get("title"):
            embed += f"\n*{attrs['title']}*"
        return embed

    text = FIGURE_RE.sub(repl_figure, text)

    def repl_md_image(m):
        alt, basename = m.group(1), m.group(2)
        new_name = copy_image(basename, post_stem, attachments_dir, report)
        if new_name is None:
            return f"<!-- broken image link: /images/post/{basename} -->"
        return f"![[attachments/{new_name}]]"

    text = MD_IMAGE_RE.sub(repl_md_image, text)

    text = HEADING_NO_SPACE_RE.sub(r"\1 \2", text)
    text = HEADING_ANCHOR_RE.sub("", text)

    return text.strip() + "\n"


# ---------- org body conversion (for the 3 orphaned drafts, no id/roam links expected) ----------

DRAWER_RE = re.compile(r":(PROPERTIES|LOGBOOK):.*?:END:\n?", re.DOTALL | re.IGNORECASE)
META_LINE_RE = re.compile(r"^#\+[A-Za-z_]+:.*$\n?", re.MULTILINE)
SRC_BLOCK_RE = re.compile(
    r"#\+begin_(\w+)(?:[ \t]+([^\n]*))?\n(.*?)#\+end_\1[ \t]*\n?", re.DOTALL | re.IGNORECASE
)
HEADLINE_RE = re.compile(r"^(\*+)\s+(?:(TODO|DONE)\s+)?(.*?)\s*(?::[A-Za-z0-9_:]+:)?\s*$")
FILE_LINK_RE = re.compile(r"\[\[file:([^\]]+?)\](?:\[([^\]]*)\])?\]")
URL_LINK_RE = re.compile(r"\[\[(https?://[^\]]+)\](?:\[([^\]]*)\])?\]")
CODE_PLACEHOLDER = "\x00CODEBLOCK{}\x00"


def extract_org_title(text: str, fallback: str) -> str:
    m = re.search(r"^#\+title:\s*(.*)$", text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else fallback


def convert_org_body(text: str, post_stem: str, src_path: Path, attachments_dir: Path, report) -> str:
    text = text.replace("\r\n", "\n")
    text = DRAWER_RE.sub("", text)
    text = META_LINE_RE.sub("", text)

    code_blocks = []

    def stash_code(m):
        block_type, args, body = m.group(1).lower(), m.group(2) or "", m.group(3)
        lang = args.split()[0] if block_type == "src" and args.split() else ""
        code_blocks.append(f"```{lang}\n{body}```\n")
        return CODE_PLACEHOLDER.format(len(code_blocks) - 1) + "\n"

    text = SRC_BLOCK_RE.sub(stash_code, text)

    out_lines = []
    for line in text.split("\n"):
        if re.match(r"^\*+\s*$", line):
            continue
        hm = HEADLINE_RE.match(line)
        if hm:
            stars, keyword, rest = hm.groups()
            kw = f"{keyword} " if keyword else ""
            out_lines.append(f"{'#' * len(stars)} {kw}{rest}".rstrip())
        else:
            out_lines.append(line)
    text = "\n".join(out_lines)

    def repl_file_link(m):
        raw_path, desc = m.group(1), m.group(2)
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (src_path.parent / raw_path).resolve()
        if not candidate.exists() or candidate.suffix.lower() not in {
            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp"
        }:
            report["missing_images"].append(raw_path)
            return f"<!-- broken image link: {raw_path} -->"
        new_name = sanitize_stem(f"{post_stem}-{candidate.stem}") + candidate.suffix.lower()
        dest = attachments_dir / new_name
        if not dest.exists():
            attachments_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, dest)
            report["images_copied"] += 1
        return f"![[attachments/{new_name}]]"

    text = FILE_LINK_RE.sub(repl_file_link, text)

    def repl_url_link(m):
        url, desc = m.group(1), m.group(2)
        return f"[{desc}]({url})" if desc else f"<{url}>"

    text = URL_LINK_RE.sub(repl_url_link, text)

    text = re.sub(r"(?<![A-Za-z0-9])\*([^\n*]+?)\*(?![A-Za-z0-9])", r"**\1**", text)
    text = re.sub(r"(?<![A-Za-z0-9])/([^\n/]+?)/(?![A-Za-z0-9])", r"*\1*", text)
    text = re.sub(r"(?<![A-Za-z0-9])~([^\n~]+?)~(?![A-Za-z0-9])", r"`\1`", text)
    text = re.sub(r"(?<![A-Za-z0-9])=([^\n=]+?)=(?![A-Za-z0-9])", r"`\1`", text)

    for i, block in enumerate(code_blocks):
        text = text.replace(CODE_PLACEHOLDER.format(i), block)

    return text.strip() + "\n"


# ---------- frontmatter emission ----------

def build_frontmatter(title, date_str, tags, publish, description=None) -> str:
    tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"
    lines = ["---", f'title: "{title}"']
    if date_str:
        lines.append(f"date: {date_str}")
    lines.append(f"tags: {tags_yaml}")
    if description:
        lines.append(f'description: "{description}"')
    lines.append(f"publish: {'true' if publish else 'false'}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ---------- main conversion passes ----------

def convert_posts(out_root: Path, limit, report):
    posts_dir = out_root / "posts"
    attachments_dir = out_root / "attachments"
    posts_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(POST_DIR.glob("*.md"))
    if limit is not None:
        files = files[:limit]

    used_stems = {}
    for f in files:
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            fm, body = parse_frontmatter(raw)

            title = fm.get("title") or f.stem
            tags = list(fm.get("tags") or []) + list(fm.get("categories") or [])
            tags = sorted({normalize_tag(t) for t in tags if t})
            draft = bool(fm.get("draft", False))
            description = fm.get("description") or None

            relpath = str(f.relative_to(SOURCE_ROOT)).replace("\\", "/")
            fm_date = stringify_date(fm.get("date"))
            date_str = resolve_date(fm_date, relpath, report)

            stem = unique_stem(slugify_filename(f.stem), used_stems)
            new_body = convert_hugo_body(body, stem, attachments_dir, report)
            content = build_frontmatter(title, date_str, tags, publish=not draft,
                                         description=description) + new_body
            (posts_dir / f"{stem}.md").write_text(content, encoding="utf-8", newline="\n")
            report["posts_converted"] += 1
        except Exception as e:  # noqa: BLE001
            report["parse_errors"].append((str(f), str(e)))


def convert_org_drafts(out_root: Path, report):
    drafts_dir = out_root / "drafts"
    attachments_dir = out_root / "attachments"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    used_stems = {}
    for name in ORPHAN_ORG_FILES:
        f = POST_DIR / name
        if not f.exists():
            report["parse_errors"].append((str(f), "orphaned org file not found"))
            continue
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            fallback_title = f.stem.replace("_", " ")
            title = extract_org_title(raw, fallback_title)

            relpath = str(f.relative_to(SOURCE_ROOT)).replace("\\", "/")
            date_str = git_first_commit_date(relpath)

            stem = unique_stem(slugify_filename(f.stem), used_stems)
            body = convert_org_body(raw, stem, f, attachments_dir, report)
            content = build_frontmatter(title, date_str, [], publish=False) + body
            (drafts_dir / f"{stem}.md").write_text(content, encoding="utf-8", newline="\n")
            report["org_drafts_converted"] += 1
        except Exception as e:  # noqa: BLE001
            report["parse_errors"].append((str(f), str(e)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", default=str(Path(r"D:\Sandbox\obsedionblog")))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    out_root = Path(args.out_root)
    report = {
        "posts_converted": 0,
        "org_drafts_converted": 0,
        "images_copied": 0,
        "missing_images": [],
        "date_overridden_by_git": [],
        "date_from_git_only": [],
        "parse_errors": [],
    }

    convert_posts(out_root, args.limit, report)
    convert_org_drafts(out_root, report)

    print(f"Source: {SOURCE_ROOT}")
    print(f"Output: {out_root}")
    print()
    print(f"Posts converted:       {report['posts_converted']}")
    print(f"Org drafts converted:  {report['org_drafts_converted']} / {len(ORPHAN_ORG_FILES)}")
    print(f"Images copied:         {report['images_copied']}")
    if report["missing_images"]:
        print(f"Missing/broken images: {len(report['missing_images'])}")
        for name in report["missing_images"]:
            print(f"  - {name}")
    if report["date_overridden_by_git"]:
        print(f"Dates overridden by git history (frontmatter disagreed by >{DATE_DISAGREEMENT_DAYS}d):")
        for relpath, fm_date, git_date in report["date_overridden_by_git"]:
            print(f"  - {relpath}: frontmatter={fm_date} -> git={git_date}")
    if report["date_from_git_only"]:
        print(f"Dates taken from git (no frontmatter date): {report['date_from_git_only']}")
    if report["parse_errors"]:
        print(f"Parse errors: {len(report['parse_errors'])}")
        for path, err in report["parse_errors"]:
            print(f"  - {path}: {err}")


if __name__ == "__main__":
    sys.exit(main())
