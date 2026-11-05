#!/usr/bin/env python3
"""commitlog — 把 git 提交历史变成 CHANGELOG，零配置。

设计目标只有一个：不需要配置文件。

用法：
    commitlog                      # 整个历史，输出到 stdout
    commitlog --unreleased         # 只看上一个 tag 之后的提交
    commitlog --bump               # 只打印下一个版本号
    commitlog --write CHANGELOG.md # 幂等写入，重复跑不会重复

核心逻辑（解析提交、归组、算版本、渲染）都是纯函数，与 git 调用完全分离，
所以测试不需要真的建仓库。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__version__ = "1.0.0"

# git log 的字段分隔符与记录分隔符。用控制字符是为了避开提交信息里可能出现的一切普通字符。
FIELD = "\x1f"
RECORD = "\x1e"
LOG_FORMAT = FIELD.join(["%H", "%h", "%an", "%ae", "%aI", "%s", "%b"]) + RECORD

# 提交类型 -> 章节标题。顺序即渲染顺序。
TYPE_TITLES = {
    "feat": "新功能",
    "fix": "问题修复",
    "perf": "性能优化",
    "refactor": "重构",
    "revert": "回滚",
    "docs": "文档",
    "build": "构建",
    "ci": "持续集成",
    "test": "测试",
    "style": "代码风格",
    "chore": "杂项",
}
OTHER_TITLE = "其他变更"
GROUP_ORDER = list(TYPE_TITLES) + ["other"]

# 默认隐藏的类型：这些提交对外部使用者没有意义，堆在 CHANGELOG 里只是噪音。
DEFAULT_HIDDEN = frozenset({"docs", "test", "build", "ci", "style", "chore"})

BREAKING_TITLE = "破坏性变更"
UNRELEASED_TITLE = "未发布"

# 语义化版本匹配。故意不写「去掉前缀」的逻辑：那样会把 version/1.2.3 削成 ersion/1.2.3。
# 这里改成在标签末尾找一个完整的版本号，前面是什么都不影响。
SEMVER_RE = re.compile(
    r"(?<![0-9.])"
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.\-]+))?"
    r"(?:\+[0-9A-Za-z.\-]+)?"
    r"$"
)

CONVENTIONAL_RE = re.compile(
    r"^(?P<kind>[A-Za-z][A-Za-z0-9_\-]*)"
    r"(?:\((?P<scope>[^()]*)\))?"
    r"(?P<bang>!)?"
    r"\s*:\s*"
    r"(?P<desc>\S.*)$"
)

BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:[ \t]*(?P<note>.*)$", re.MULTILINE | re.IGNORECASE)

PR_RE = re.compile(r"\(#(\d+)\)\s*$")


class CommitlogError(Exception):
    """给用户看的错误。消息里必须写清楚「怎么办」。"""


# --------------------------------------------------------------------------
# 版本号
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @property
    def core(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __str__(self) -> str:
        if self.pre:
            return f"{self.core}-{'.'.join(self.pre)}"
        return self.core

    def sort_key(self) -> tuple:
        # 没有预发布标记的版本比有标记的大（1.0.0 > 1.0.0-rc.1）。
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, ids)


def parse_version(tag: str) -> Version | None:
    """从标签名里取出语义化版本号；取不出来就返回 None。

    >>> parse_version("v1.2.3")
    Version(major=1, minor=2, patch=3, pre=())
    >>> parse_version("version/1.2.3")
    Version(major=1, minor=2, patch=3, pre=())
    >>> parse_version("nightly") is None
    True
    """
    m = SEMVER_RE.search(tag.strip())
    if not m:
        return None
    pre = tuple(p for p in (m.group("pre") or "").split(".") if p)
    return Version(int(m.group("major")), int(m.group("minor")), int(m.group("patch")), pre)


def highest_version(tags: Iterable[str], *, stable_only: bool = False) -> Version | None:
    versions = []
    for tag in tags:
        v = parse_version(tag)
        if v is None:
            continue
        if stable_only and v.pre:
            continue
        versions.append(v)
    if not versions:
        return None
    return max(versions, key=Version.sort_key)


def bump_core(last: Version | None, commits: Sequence["Commit"]) -> Version:
    """按 conventional commits 的约定推算下一个正式版本号。

    - 有破坏性变更：主版本 +1；但主版本是 0 时只进次版本（0.x 还没定型）
    - 有 feat：次版本 +1
    - 其余（fix / perf / 无类型）：修订号 +1
    """
    if last is None:
        return Version(0, 1, 0)
    if any(c.breaking for c in commits):
        if last.major == 0:
            return Version(0, last.minor + 1, 0)
        return Version(last.major + 1, 0, 0)
    if any(c.kind == "feat" for c in commits):
        return Version(last.major, last.minor + 1, 0)
    return Version(last.major, last.minor, last.patch + 1)


def next_version(last: Version | None, commits: Sequence["Commit"], pre_label: str | None = None) -> str:
    """算出下一个版本号，返回字符串。

    pre_label 为空时走正式版；给了 pre_label 就产出 1.3.0-alpha.1 这样的预发布版本。
    如果上一个版本已经是同一条预发布序列（1.3.0-alpha.2），只把序号 +1。
    """
    if pre_label:
        label = pre_label.strip().lower()
        if last is not None and last.pre:
            if last.pre[0] == label:
                # 同一条预发布序列：核心版本不动，序号 +1
                rest = last.pre[1:]
                num = 1
                if rest and rest[-1].isdigit():
                    num = int(rest[-1]) + 1
                    rest = rest[:-1]
                return f"{last.core}-{'.'.join((label,) + rest + (str(num),))}"
            # 换了预发布标签：还在准备同一个核心版本，不额外进版本
            return f"{last.core}-{label}.1"
        core = bump_core(last, commits)
        return f"{core}-{label}.1"

    if last is not None and last.pre:
        # 上一个标签是预发布，这次正式发布 = 把它转正，不再进版本
        return last.core
    return str(bump_core(last, commits))


# --------------------------------------------------------------------------
# 提交解析
# --------------------------------------------------------------------------


@dataclass
class Conventional:
    kind: str
    scope: str
    breaking: bool
    description: str
    note: str


def parse_conventional(subject: str, body: str = "") -> Conventional | None:
    """解析 `type(scope)!: 描述` 形式的提交标题。解析不了返回 None。

    >>> c = parse_conventional("feat(api)!: 支持分页")
    >>> (c.kind, c.scope, c.breaking, c.description)
    ('feat', 'api', True, '支持分页')
    """
    subject = subject.strip()
    if not subject:
        return None
    m = CONVENTIONAL_RE.match(subject)
    if not m:
        return None

    kind = m.group("kind").lower()
    scope = (m.group("scope") or "").strip()
    description = m.group("desc").strip()
    breaking = bool(m.group("bang"))
    note = ""

    bm = BREAKING_RE.search(body or "")
    if bm:
        breaking = True
        note = bm.group("note").strip()

    if breaking and not note:
        # `!` 标了破坏性但正文没写细节，用标题本身当说明
        note = description

    return Conventional(kind=kind, scope=scope, breaking=breaking, description=description, note=note)


@dataclass
class Commit:
    sha: str
    short: str
    author: str
    email: str
    date: str
    subject: str
    body: str = ""
    tags: tuple[str, ...] = ()
    kind: str = "other"
    scope: str = ""
    breaking: bool = False
    description: str = ""
    note: str = ""
    conventional: bool = False

    def __post_init__(self) -> None:
        parsed = parse_conventional(self.subject, self.body)
        if parsed is None:
            self.kind = "other"
            self.description = self.subject.strip()
            self.breaking = bool(BREAKING_RE.search(self.body or ""))
            if self.breaking:
                self.note = BREAKING_RE.search(self.body).group("note").strip() or self.description
            return
        self.conventional = True
        self.kind = parsed.kind if parsed.kind in TYPE_TITLES else "other"
        self.scope = parsed.scope
        self.breaking = parsed.breaking
        self.description = parsed.description
        self.note = parsed.note

    @property
    def day(self) -> str:
        return (self.date or "")[:10]

    @property
    def title(self) -> str:
        return TYPE_TITLES.get(self.kind, OTHER_TITLE)


def parse_log(raw: str) -> list[Commit]:
    """把 `git log --format=...` 的原始输出切成提交列表（保持原顺序）。"""
    commits: list[Commit] = []
    for chunk in raw.split(RECORD):
        chunk = chunk.strip("\r\n")
        if not chunk.strip():
            continue
        parts = chunk.split(FIELD)
        if len(parts) < 7:
            parts += [""] * (7 - len(parts))
        sha, short, author, email, date, subject, body = parts[:7]
        commits.append(
            Commit(
                sha=sha.strip(),
                short=short.strip(),
                author=author.strip(),
                email=email.strip(),
                date=date.strip(),
                subject=subject.strip(),
                body=body.strip("\n"),
            )
        )
    return commits


# --------------------------------------------------------------------------
# 分组成版本
# --------------------------------------------------------------------------


@dataclass
class Release:
    version: Version | None
    commits: list[Commit] = field(default_factory=list)

    @property
    def is_unreleased(self) -> bool:
        return self.version is None

    @property
    def tag(self) -> str:
        return str(self.version) if self.version else ""

    @property
    def date(self) -> str:
        return self.commits[0].day if self.commits else ""


def attach_tags(commits: Sequence[Commit], tags: dict[str, list[str]]) -> None:
    """把 tag 挂到对应提交上。tags 的键是完整 sha。"""
    for c in commits:
        found = tags.get(c.sha)
        if found:
            c.tags = tuple(sorted(found))


def build_releases(commits: Sequence[Commit]) -> list[Release]:
    """按 tag 把提交切成一个个版本段，最新的在前。

    规则：某条提交身上有版本标签，就从它开始一个新版本段，它本身算进这个版本。
    """
    releases: list[Release] = []
    current = Release(version=None)
    for c in commits:
        v = highest_version(c.tags)
        if v is not None:
            if current.commits:
                releases.append(current)
            current = Release(version=v, commits=[c])
            continue
        current.commits.append(c)
    if current.commits:
        releases.append(current)
    return releases


def visible_commits(rel: Release, hidden: frozenset[str], strict: bool) -> list[Commit]:
    out = []
    for c in rel.commits:
        if c.kind in hidden:
            continue
        if strict and not c.conventional:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


@dataclass
class Options:
    repo_url: str | None = None
    links: bool = True
    hidden: frozenset[str] = DEFAULT_HIDDEN
    strict: bool = False
    title: str = "Changelog"

    def commit_url(self, sha: str) -> str | None:
        if not self.links or not self.repo_url:
            return None
        return f"{self.repo_url.rstrip('/')}/commit/{sha}"


def _linkify_pr(text: str, opts: Options) -> str:
    if not opts.links or not opts.repo_url:
        return text
    m = PR_RE.search(text)
    if not m:
        return text
    num = m.group(1)
    return PR_RE.sub(f"([#{num}]({opts.repo_url.rstrip('/')}/pull/{num}))", text)


def _commit_line(c: Commit, opts: Options) -> str:
    desc = " ".join(c.description.split())
    desc = _linkify_pr(desc, opts)
    prefix = f"**{c.scope}**: " if c.scope else ""
    url = opts.commit_url(c.sha)
    sha = f"([{c.short}]({url}))" if url else f"({c.short})"
    return f"- {prefix}{desc} {sha}"


def _breaking_line(c: Commit) -> str:
    desc = " ".join(c.description.split())
    note = " ".join((c.note or "").split())
    if note and note != desc:
        return f"- {desc} —— {note}"
    return f"- {desc}"


def release_heading(rel: Release) -> str:
    if rel.is_unreleased:
        return f"## {UNRELEASED_TITLE}"
    if rel.date:
        return f"## [{rel.version}] - {rel.date}"
    return f"## [{rel.version}]"


def section_key(rel: Release) -> str:
    """用于幂等写入时识别同一段落的键，不含日期。"""
    if rel.is_unreleased:
        return f"## {UNRELEASED_TITLE}"
    return f"## [{rel.version}]"


def render_release(rel: Release, opts: Options) -> str:
    commits = visible_commits(rel, opts.hidden, opts.strict)
    lines: list[str] = [release_heading(rel)]

    breaking = [c for c in commits if c.breaking]
    if breaking:
        lines.append("")
        lines.append(f"### {BREAKING_TITLE}")
        lines.append("")
        lines.extend(_breaking_line(c) for c in breaking)

    groups: dict[str, list[Commit]] = {}
    for c in commits:
        groups.setdefault(c.kind, []).append(c)

    for kind in GROUP_ORDER:
        items = groups.get(kind)
        if not items:
            continue
        lines.append("")
        lines.append(f"### {TYPE_TITLES.get(kind, OTHER_TITLE)}")
        lines.append("")
        # 同一章节内按时间正序，读起来像一条时间线
        for c in sorted(items, key=lambda x: x.date):
            lines.append(_commit_line(c, opts))

    if len(lines) == 1:
        lines.append("")
        lines.append("（只有内部变更，没有对外可见的条目）")

    return "\n".join(lines).rstrip() + "\n"


def render(releases: Sequence[Release], opts: Options) -> str:
    blocks = []
    for rel in releases:
        if not visible_commits(rel, opts.hidden, opts.strict) and not rel.is_unreleased:
            continue
        blocks.append(render_release(rel, opts))
    body = "\n".join(blocks).rstrip()
    header = f"# {opts.title}"
    if not body:
        return f"{header}\n\n（这个区间没有值得记录的提交）\n"
    return f"{header}\n\n{body}\n"


# --------------------------------------------------------------------------
# 幂等写入
# --------------------------------------------------------------------------


def default_header(title: str = "Changelog") -> str:
    return f"# {title}\n\n所有值得注意的变更都会记录在这里。\n"


def split_changelog(text: str) -> tuple[list[str], list[list[str]]]:
    lines = text.replace("\r\n", "\n").split("\n")
    preamble: list[str] = []
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for ln in lines:
        if ln.startswith("## "):
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is None:
            preamble.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        blocks.append(cur)
    return preamble, blocks


def join_changelog(preamble: list[str], blocks: list[list[str]]) -> str:
    parts = []
    pre = "\n".join(preamble).strip()
    if pre:
        parts.append(pre)
    for b in blocks:
        text = "\n".join(b).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts) + "\n"


def upsert_section(text: str, key: str, section: str) -> str:
    """把一段渲染结果放进 CHANGELOG：同版本的旧段落替换，新版本插到最前面。

    这样同一个版本反复生成也不会出现两份，重跑是安全的。
    """
    preamble, blocks = split_changelog(text)
    new_block = section.strip("\n").split("\n")
    for i, b in enumerate(blocks):
        if b and b[0].startswith(key):
            blocks[i] = new_block
            return join_changelog(preamble, blocks)
    blocks.insert(0, new_block)
    return join_changelog(preamble, blocks)


# --------------------------------------------------------------------------
# git 交互
# --------------------------------------------------------------------------


def _run_git(args: Sequence[str], cwd: str | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise CommitlogError("找不到 git 命令。先装 git，并确认它在 PATH 里。") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip()
        if "not a git repository" in msg:
            where = cwd or os.getcwd()
            raise CommitlogError(f"{where} 不是 git 仓库。在仓库目录里运行，或者先 git init。")
        raise CommitlogError(f"git {args[0]} 执行失败：{msg or '没有输出'}")
    return proc.stdout


def read_log(rev_range: str | None = None, include_merges: bool = False, cwd: str | None = None) -> list[Commit]:
    args = ["log", f"--format={LOG_FORMAT}", "--date-order"]
    if not include_merges:
        args.append("--no-merges")
    if rev_range:
        args.append(rev_range)
    try:
        raw = _run_git(args, cwd)
    except CommitlogError as exc:
        # 仓库刚 init、一个提交都还没有时，git log 会直接报错。这不算错误。
        if "does not have any commits yet" in str(exc) or "unknown revision" in str(exc):
            return []
        raise
    return parse_log(raw)


def read_tags(cwd: str | None = None) -> dict[str, list[str]]:
    """返回 {提交 sha: [标签名, ...]}。

    %(*objectname) 是附注标签指向的提交，轻量标签这里为空，所以两者都要看。
    """
    raw = _run_git(["for-each-ref", "refs/tags", "--format=%(refname:short)%09%(objectname)%09%(*objectname)"], cwd)
    mapping: dict[str, list[str]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, obj, deref = parts[0].strip(), parts[1].strip(), parts[2].strip()
        sha = deref or obj
        if name and sha:
            mapping.setdefault(sha, []).append(name)
    return mapping


def detect_repo_url(cwd: str | None = None) -> str | None:
    try:
        url = _run_git(["remote", "get-url", "origin"], cwd).strip()
    except CommitlogError:
        return None
    if not url:
        return None
    if url.startswith("git@"):
        host, _, path = url[4:].partition(":")
        return f"https://{host}/{path}"
    if url.startswith("ssh://git@"):
        rest = url[len("ssh://git@"):]
        host, _, path = rest.partition("/")
        return f"https://{host}/{path}"
    return url


def normalize_repo_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

EXAMPLES = """\
例子：
  commitlog                                  整个历史打到屏幕
  commitlog --unreleased                     只看上一个 tag 之后
  commitlog --write CHANGELOG.md             写进文件，重跑不会重复
  commitlog --bump                           只打印下一个版本号
  commitlog --bump --pre beta                下一个预发布版本号
  commitlog v1.2.0..HEAD                     指定区间
  commitlog --all                            连 docs/chore 一起列
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="commitlog",
        description="把 git 提交历史变成 CHANGELOG，不需要配置文件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    p.add_argument("rev_range", nargs="?", metavar="REV-RANGE", help="传给 git log 的区间，例如 v1.0.0..HEAD")
    p.add_argument("--since", metavar="REF", help="只处理这个引用之后的提交（等价于 REF..HEAD）")
    p.add_argument("--bump", action="store_true", help="只打印下一个版本号，不输出变更内容")
    p.add_argument("--pre", metavar="LABEL", help="配合 --bump 生成预发布版本，例如 --pre alpha")
    p.add_argument("--unreleased", action="store_true", help="只输出未发布段落")
    p.add_argument("--write", metavar="PATH", help="写入 CHANGELOG 文件（同版本段落会被替换）")
    p.add_argument("--all", action="store_true", help="连 docs / chore / ci 这类内部提交也列出来")
    p.add_argument("--hide", metavar="TYPES", help="额外隐藏的类型，逗号分隔")
    p.add_argument("--strict", action="store_true", help="只保留符合 Conventional Commits 的提交")
    p.add_argument("--merges", action="store_true", help="把合并提交也算进来")
    p.add_argument("--repo", metavar="URL", help="链接用的仓库地址，默认从 origin 推断")
    p.add_argument("--no-links", action="store_true", help="不生成任何链接")
    p.add_argument("--title", default="Changelog", metavar="TEXT", help="一级标题文字")
    p.add_argument("--version", action="version", version=f"commitlog {__version__}")
    return p


def _parse_type_list(value: str | None) -> set[str]:
    if not value:
        return set()
    return {x.strip().lower() for x in value.split(",") if x.strip()}


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    args = build_parser().parse_args(argv)

    if args.rev_range and args.since:
        print("--since 和区间参数只能给一个。", file=sys.stderr)
        return 2

    rev_range = args.rev_range or (f"{args.since}..HEAD" if args.since else None)

    try:
        commits = read_log(rev_range, include_merges=args.merges)
        if not commits:
            print("这个区间里没有提交，没什么可记录的。", file=sys.stderr)
            return 0
        attach_tags(commits, read_tags())
        releases = build_releases(commits)

        if args.bump:
            tagged = [r.version for r in releases if r.version is not None]
            last = max(tagged, key=Version.sort_key) if tagged else None
            pending = releases[0].commits if releases[0].is_unreleased else []
            print(next_version(last, pending, args.pre))
            return 0

        hidden = set() if args.all else set(DEFAULT_HIDDEN)
        hidden |= _parse_type_list(args.hide)
        if args.no_links:
            repo_url = None
        else:
            repo_url = args.repo or detect_repo_url()
            repo_url = normalize_repo_url(repo_url) if repo_url else None

        opts = Options(
            repo_url=repo_url,
            links=not args.no_links,
            hidden=frozenset(hidden),
            strict=args.strict,
            title=args.title,
        )

        selected = [r for r in releases if r.is_unreleased] if args.unreleased else list(releases)
        if not selected:
            print("还没有未发布的提交。", file=sys.stderr)
            return 0

        if args.write:
            path = args.write
            existing = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    existing = fh.read()
            else:
                existing = default_header(args.title)
            text = existing
            wrote = 0
            # 倒着写：每次新段落都插到最前面，倒序插入之后最新的才会落在最上面
            for rel in reversed(selected):
                if not visible_commits(rel, opts.hidden, opts.strict) and not rel.is_unreleased:
                    continue
                text = upsert_section(text, section_key(rel), render_release(rel, opts))
                wrote += 1
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            print(f"已写入 {path}（{wrote} 个版本段落）")
            return 0

        sys.stdout.write(render(selected, opts))
        return 0

    except CommitlogError as exc:
        print(f"出错了：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
