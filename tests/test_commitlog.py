"""commitlog 的测试。

核心逻辑全部是纯函数，所以大部分用例不需要真的建 git 仓库。
最后一组用例会在临时目录里建一个真仓库，验证从 git 到文本的完整链路。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commitlog as cl  # noqa: E402


def commit(subject, body="", sha="a" * 40, short="aaaaaaa", date="2026-10-20T10:00:00+08:00", tags=()):
    return cl.Commit(
        sha=sha,
        short=short,
        author="赵旭",
        email="2210914541@qq.com",
        date=date,
        subject=subject,
        body=body,
        tags=tuple(tags),
    )


# --------------------------------------------------------------------------
# 版本号
# --------------------------------------------------------------------------


class VersionTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(str(cl.parse_version("1.2.3")), "1.2.3")

    def test_v_prefix(self):
        self.assertEqual(str(cl.parse_version("v1.2.3")), "1.2.3")

    def test_slash_prefix(self):
        # 回归：早期版本会把 version/1.2.3 削成 ersion/1.2.3
        self.assertEqual(str(cl.parse_version("version/1.2.3")), "1.2.3")

    def test_hyphen_prefix(self):
        self.assertEqual(str(cl.parse_version("release-1.2.3")), "1.2.3")

    def test_monorepo_scope(self):
        self.assertEqual(str(cl.parse_version("pkg-core@2.0.1")), "2.0.1")

    def test_prerelease(self):
        v = cl.parse_version("v2.0.0-rc.1")
        self.assertEqual((v.major, v.minor, v.patch), (2, 0, 0))
        self.assertEqual(v.pre, ("rc", "1"))

    def test_build_metadata_ignored(self):
        self.assertEqual(str(cl.parse_version("v1.2.3+build.7")), "1.2.3")

    def test_date_like_tag(self):
        self.assertIsNotNone(cl.parse_version("2026.10.25"))

    def test_two_part_is_not_version(self):
        self.assertIsNone(cl.parse_version("v1.2"))

    def test_four_part_is_not_version(self):
        self.assertIsNone(cl.parse_version("1.2.3.4"))

    def test_word_is_not_version(self):
        self.assertIsNone(cl.parse_version("nightly"))

    def test_version_word_only(self):
        # 标签名字面就叫 version，不该被当成版本号
        self.assertIsNone(cl.parse_version("version"))

    def test_prerelease_sorts_below_release(self):
        self.assertLess(cl.parse_version("1.0.0-rc.1").sort_key(), cl.parse_version("1.0.0").sort_key())

    def test_prerelease_number_order(self):
        self.assertLess(cl.parse_version("1.0.0-rc.2").sort_key(), cl.parse_version("1.0.0-rc.10").sort_key())

    def test_highest_version(self):
        tags = ["v1.0.0", "v2.1.0", "v2.0.9", "nightly"]
        self.assertEqual(str(cl.highest_version(tags)), "2.1.0")

    def test_highest_version_stable_only(self):
        tags = ["v1.0.0", "v2.0.0-rc.1"]
        self.assertEqual(str(cl.highest_version(tags, stable_only=True)), "1.0.0")

    def test_highest_version_empty(self):
        self.assertIsNone(cl.highest_version(["nightly"]))


# --------------------------------------------------------------------------
# Conventional Commits 解析
# --------------------------------------------------------------------------


class ParseConventionalTests(unittest.TestCase):
    def test_basic(self):
        c = cl.parse_conventional("feat: 支持分页")
        self.assertEqual((c.kind, c.scope, c.breaking, c.description), ("feat", "", False, "支持分页"))

    def test_scope(self):
        c = cl.parse_conventional("fix(api): 修掉空指针")
        self.assertEqual(c.scope, "api")

    def test_bang_marks_breaking(self):
        c = cl.parse_conventional("feat(api)!: 换掉响应格式")
        self.assertTrue(c.breaking)

    def test_uppercase_type_lowered(self):
        self.assertEqual(cl.parse_conventional("Feat: x").kind, "feat")

    def test_type_with_hyphen(self):
        self.assertEqual(cl.parse_conventional("code-style: x").kind, "code-style")

    def test_no_colon(self):
        self.assertIsNone(cl.parse_conventional("Update readme"))

    def test_empty_description(self):
        self.assertIsNone(cl.parse_conventional("feat: "))

    def test_empty_subject(self):
        self.assertIsNone(cl.parse_conventional("   "))

    def test_breaking_footer(self):
        c = cl.parse_conventional("feat: 换接口", "BREAKING CHANGE: 老字段全删了")
        self.assertTrue(c.breaking)
        self.assertEqual(c.note, "老字段全删了")

    def test_breaking_footer_hyphen(self):
        c = cl.parse_conventional("fix: x", "BREAKING-CHANGE: 配置项改名")
        self.assertTrue(c.breaking)

    def test_breaking_footer_multiline_body(self):
        body = "改了一些内部实现。\n\nBREAKING CHANGE: 返回值从 list 变成 dict\n"
        c = cl.parse_conventional("refactor: 收拢返回值", body)
        self.assertEqual(c.note, "返回值从 list 变成 dict")

    def test_bang_without_note_falls_back_to_description(self):
        c = cl.parse_conventional("feat!: 删掉旧命令")
        self.assertEqual(c.note, "删掉旧命令")

    def test_scope_with_space_inside(self):
        c = cl.parse_conventional("fix(a b): x")
        self.assertEqual(c.scope, "a b")

    def test_description_keeps_colon(self):
        c = cl.parse_conventional("docs: 说明 a: b 的用法")
        self.assertEqual(c.description, "说明 a: b 的用法")


class CommitTests(unittest.TestCase):
    def test_known_kind(self):
        c = commit("feat: x")
        self.assertEqual((c.kind, c.title), ("feat", "新功能"))

    def test_unknown_kind_goes_to_other(self):
        c = commit("wip: 随便写写")
        self.assertEqual(c.kind, "other")

    def test_non_conventional_subject_kept(self):
        c = commit("Update readme")
        self.assertEqual(c.description, "Update readme")
        self.assertFalse(c.conventional)

    def test_day(self):
        self.assertEqual(commit("feat: x").day, "2026-10-20")

    def test_body_breaking_marks_commit(self):
        c = commit("feat: x", "BREAKING CHANGE: 删字段")
        self.assertTrue(c.breaking)
        self.assertEqual(c.note, "删字段")


class ParseLogTests(unittest.TestCase):
    def test_two_records(self):
        raw = (
            f"sha1{cl.FIELD}s1{cl.FIELD}张三{cl.FIELD}a@b.c{cl.FIELD}2026-10-01T00:00:00+08:00"
            f"{cl.FIELD}feat: 一{cl.FIELD}{cl.RECORD}\n"
            f"sha2{cl.FIELD}s2{cl.FIELD}李四{cl.FIELD}d@e.f{cl.FIELD}2026-10-02T00:00:00+08:00"
            f"{cl.FIELD}fix: 二{cl.FIELD}正文{cl.RECORD}\n"
        )
        commits = cl.parse_log(raw)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0].subject, "feat: 一")
        self.assertEqual(commits[1].body, "正文")

    def test_empty_body(self):
        raw = f"sha{cl.FIELD}s{cl.FIELD}a{cl.FIELD}e{cl.FIELD}2026-10-01T00:00:00+08:00{cl.FIELD}feat: x{cl.FIELD}{cl.RECORD}"
        commits = cl.parse_log(raw)
        self.assertEqual(commits[0].body, "")

    def test_blank_input(self):
        self.assertEqual(cl.parse_log("\n\n"), [])

    def test_truncated_record_does_not_crash(self):
        raw = f"sha{cl.FIELD}s{cl.FIELD}a{cl.RECORD}"
        commits = cl.parse_log(raw)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].subject, "")


# --------------------------------------------------------------------------
# 版本分段
# --------------------------------------------------------------------------


class BuildReleasesTests(unittest.TestCase):
    def test_single_unreleased(self):
        releases = cl.build_releases([commit("feat: a"), commit("fix: b")])
        self.assertEqual(len(releases), 1)
        self.assertTrue(releases[0].is_unreleased)

    def test_tag_starts_new_section(self):
        commits = [
            commit("fix: 新的一条", date="2026-10-03T00:00:00+08:00"),
            commit("feat: 老的", date="2026-10-02T00:00:00+08:00", tags=["v1.0.0"]),
            commit("chore: 更老", date="2026-10-01T00:00:00+08:00"),
        ]
        cl.attach_tags(commits, {})
        releases = cl.build_releases(commits)
        self.assertEqual(len(releases), 2)
        self.assertTrue(releases[0].is_unreleased)
        self.assertEqual(str(releases[1].version), "1.0.0")
        # 打了标签的那条提交属于它自己的版本
        self.assertEqual(releases[1].commits[0].subject, "feat: 老的")
        self.assertEqual(len(releases[1].commits), 2)

    def test_multiple_tags_pick_highest(self):
        commits = [commit("feat: x", tags=["v1.0.0", "v1.2.0"])]
        releases = cl.build_releases(commits)
        self.assertEqual(str(releases[0].version), "1.2.0")

    def test_non_version_tag_ignored(self):
        commits = [commit("feat: x", tags=["nightly"])]
        releases = cl.build_releases(commits)
        self.assertTrue(releases[0].is_unreleased)

    def test_release_date_from_newest_commit(self):
        commits = [
            commit("fix: b", date="2026-10-05T09:00:00+08:00", tags=["v2.0.0"]),
            commit("fix: a", date="2026-10-01T09:00:00+08:00"),
        ]
        releases = cl.build_releases(commits)
        self.assertEqual(releases[0].date, "2026-10-05")


class VisibleCommitsTests(unittest.TestCase):
    def test_default_hides_chore(self):
        rel = cl.Release(version=None, commits=[commit("feat: a"), commit("chore: b")])
        self.assertEqual([c.kind for c in cl.visible_commits(rel, cl.DEFAULT_HIDDEN, False)], ["feat"])

    def test_strict_drops_unparsed(self):
        rel = cl.Release(version=None, commits=[commit("feat: a"), commit("Update readme")])
        self.assertEqual(len(cl.visible_commits(rel, frozenset(), True)), 1)

    def test_nothing_hidden(self):
        rel = cl.Release(version=None, commits=[commit("chore: b")])
        self.assertEqual(len(cl.visible_commits(rel, frozenset(), False)), 1)


# --------------------------------------------------------------------------
# 版本推算
# --------------------------------------------------------------------------


class BumpTests(unittest.TestCase):
    def test_first_release(self):
        self.assertEqual(str(cl.bump_core(None, [commit("feat: x")])), "0.1.0")

    def test_breaking_major(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(str(cl.bump_core(v, [commit("feat!: x")])), "2.0.0")

    def test_breaking_on_zero_stays_minor(self):
        v = cl.parse_version("0.4.1")
        self.assertEqual(str(cl.bump_core(v, [commit("feat!: x")])), "0.5.0")

    def test_feature_minor(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(str(cl.bump_core(v, [commit("feat: x")])), "1.3.0")

    def test_fix_patch(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(str(cl.bump_core(v, [commit("fix: x")])), "1.2.4")

    def test_perf_patch(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(str(cl.bump_core(v, [commit("perf: x")])), "1.2.4")

    def test_no_commits_patch(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(str(cl.bump_core(v, [])), "1.2.4")

    def test_breaking_beats_feature(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(str(cl.bump_core(v, [commit("feat: x"), commit("fix!: y")])), "2.0.0")

    def test_promote_prerelease(self):
        v = cl.parse_version("1.3.0-rc.2")
        self.assertEqual(cl.next_version(v, [commit("fix: x")]), "1.3.0")

    def test_start_prerelease_series(self):
        v = cl.parse_version("1.2.3")
        self.assertEqual(cl.next_version(v, [commit("feat: x")], "beta"), "1.3.0-beta.1")

    def test_continue_prerelease_series(self):
        v = cl.parse_version("1.3.0-beta.1")
        self.assertEqual(cl.next_version(v, [commit("fix: x")], "beta"), "1.3.0-beta.2")

    def test_continue_prerelease_with_numeric_tail(self):
        v = cl.parse_version("1.3.0-beta.9")
        self.assertEqual(cl.next_version(v, [commit("fix: x")], "beta"), "1.3.0-beta.10")

    def test_different_label_restarts_series(self):
        v = cl.parse_version("1.3.0-beta.4")
        self.assertEqual(cl.next_version(v, [commit("fix: x")], "rc"), "1.3.0-rc.1")

    def test_prerelease_without_tag(self):
        self.assertEqual(cl.next_version(None, [commit("feat: x")], "alpha"), "0.1.0-alpha.1")


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.opts = cl.Options(repo_url="https://github.com/o/r", links=True)

    def test_heading_with_date(self):
        rel = cl.Release(version=cl.parse_version("1.2.0"), commits=[commit("feat: x", date="2026-10-20T00:00:00+08:00")])
        self.assertEqual(cl.release_heading(rel), "## [1.2.0] - 2026-10-20")

    def test_unreleased_heading(self):
        rel = cl.Release(version=None, commits=[commit("feat: x")])
        self.assertEqual(cl.release_heading(rel), "## 未发布")

    def test_group_title(self):
        rel = cl.Release(version=None, commits=[commit("feat: 加功能")])
        out = cl.render_release(rel, self.opts)
        self.assertIn("### 新功能", out)
        self.assertIn("加功能", out)

    def test_scope_is_bold(self):
        rel = cl.Release(version=None, commits=[commit("fix(api): 修掉空指针")])
        self.assertIn("**api**: 修掉空指针", cl.render_release(rel, self.opts))

    def test_commit_link(self):
        rel = cl.Release(version=None, commits=[commit("feat: x", sha="b" * 40, short="bbbbbbb")])
        self.assertIn(f"(https://github.com/o/r/commit/{'b' * 40})", cl.render_release(rel, self.opts))

    def test_no_links(self):
        opts = cl.Options(links=False)
        rel = cl.Release(version=None, commits=[commit("feat: x")])
        out = cl.render_release(rel, opts)
        self.assertNotIn("http", out)

    def test_pr_number_linked(self):
        rel = cl.Release(version=None, commits=[commit("fix: 修掉崩溃 (#42)")])
        out = cl.render_release(rel, self.opts)
        self.assertIn("[#42](https://github.com/o/r/pull/42)", out)

    def test_breaking_section_first(self):
        rel = cl.Release(version=None, commits=[commit("feat: 普通"), commit("feat!: 破坏")])
        out = cl.render_release(rel, self.opts)
        self.assertLess(out.index("破坏性变更"), out.index("新功能"))

    def test_breaking_note_shown(self):
        rel = cl.Release(version=None, commits=[commit("feat: x", "BREAKING CHANGE: 字段改名")])
        self.assertIn("字段改名", cl.render_release(rel, self.opts))

    def test_group_order_follows_constant(self):
        opts = cl.Options(hidden=frozenset())
        rel = cl.Release(version=None, commits=[commit("docs: 文档"), commit("fix: 修"), commit("feat: 加")])
        out = cl.render_release(rel, opts)
        self.assertLess(out.index("### 新功能"), out.index("### 问题修复"))
        self.assertLess(out.index("### 问题修复"), out.index("### 文档"))

    def test_items_sorted_oldest_first(self):
        rel = cl.Release(
            version=None,
            commits=[
                commit("feat: 新的", date="2026-10-09T00:00:00+08:00"),
                commit("feat: 旧的", date="2026-10-01T00:00:00+08:00"),
            ],
        )
        out = cl.render_release(rel, self.opts)
        self.assertLess(out.index("旧的"), out.index("新的"))

    def test_empty_release_note(self):
        rel = cl.Release(version=None, commits=[commit("chore: x")])
        self.assertIn("没有对外可见的条目", cl.render_release(rel, self.opts))

    def test_render_has_title(self):
        rel = cl.Release(version=None, commits=[commit("feat: x")])
        self.assertTrue(cl.render([rel], self.opts).startswith("# Changelog"))

    def test_render_custom_title(self):
        opts = cl.Options(title="发布记录")
        rel = cl.Release(version=None, commits=[commit("feat: x")])
        self.assertTrue(cl.render([rel], opts).startswith("# 发布记录"))

    def test_render_skips_empty_tagged_release(self):
        releases = [
            cl.Release(version=None, commits=[commit("feat: x")]),
            cl.Release(version=cl.parse_version("1.0.0"), commits=[commit("chore: y")]),
        ]
        out = cl.render(releases, self.opts)
        self.assertNotIn("1.0.0", out)

    def test_render_nothing(self):
        self.assertIn("没有值得记录的提交", cl.render([], self.opts))


# --------------------------------------------------------------------------
# 幂等写入
# --------------------------------------------------------------------------


class UpsertTests(unittest.TestCase):
    def test_into_empty_file(self):
        text = cl.upsert_section(cl.default_header(), "## 未发布", "## 未发布\n\n### 新功能\n\n- a\n")
        self.assertIn("# Changelog", text)
        self.assertIn("## 未发布", text)

    def test_section_at_top(self):
        text = cl.upsert_section("# Changelog\n\n## [1.0.0] - 2026-01-01\n\n- old\n", "## 未发布", "## 未发布\n\n- new\n")
        self.assertLess(text.index("## 未发布"), text.index("## [1.0.0]"))

    def test_same_version_replaced(self):
        base = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n- old\n"
        once = cl.upsert_section(base, "## [1.0.0]", "## [1.0.0] - 2026-02-02\n\n- new\n")
        self.assertIn("- new", once)
        self.assertNotIn("- old", once)

    def test_idempotent(self):
        base = "# Changelog\n\n## 未发布\n\n- 旧内容\n"
        once = cl.upsert_section(base, "## 未发布", "## 未发布\n\n- 新内容\n")
        twice = cl.upsert_section(once, "## 未发布", "## 未发布\n\n- 新内容\n")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("## 未发布"), 1)

    def test_keeps_unrelated_sections(self):
        base = "# Changelog\n\n## [2.0.0] - 2026-03-01\n\n- two\n\n## [1.0.0] - 2026-01-01\n\n- one\n"
        out = cl.upsert_section(base, "## 未发布", "## 未发布\n\n- fresh\n")
        self.assertIn("- two", out)
        self.assertIn("- one", out)

    def test_crlf_input(self):
        base = "# Changelog\r\n\r\n## 未发布\r\n\r\n- old\r\n"
        out = cl.upsert_section(base, "## 未发布", "## 未发布\n\n- new\n")
        self.assertNotIn("\r", out)

    def test_section_key_ignores_date(self):
        rel = cl.Release(version=cl.parse_version("1.2.3"), commits=[commit("feat: x")])
        self.assertEqual(cl.section_key(rel), "## [1.2.3]")


# --------------------------------------------------------------------------
# 端到端：真仓库
# --------------------------------------------------------------------------


def _git(cwd, *args, env=None):
    full_env = os.environ.copy()
    full_env.update(env or {})
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=full_env)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout


@unittest.skipIf(shutil.which("git") is None, "没有 git，跳过端到端用例")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="commitlog-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _git(self.tmp, "init", "-b", "main")
        _git(self.tmp, "config", "user.name", "赵旭")
        _git(self.tmp, "config", "user.email", "2210914541@qq.com")
        _git(self.tmp, "config", "commit.gpgsign", "false")

    def _commit(self, message, body="", day="2026-10-01"):
        path = os.path.join(self.tmp, "file.txt")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        _git(self.tmp, "add", "file.txt")
        args = ["commit", "-m", message]
        if body:
            args += ["-m", body]
        _git(
            self.tmp,
            *args,
            env={
                "GIT_AUTHOR_DATE": f"{day}T10:00:00+08:00",
                "GIT_COMMITTER_DATE": f"{day}T10:00:00+08:00",
            },
        )

    def _tag(self, name, day="2026-10-05"):
        _git(
            self.tmp,
            "tag",
            name,
            env={
                "GIT_AUTHOR_DATE": f"{day}T10:00:00+08:00",
                "GIT_COMMITTER_DATE": f"{day}T10:00:00+08:00",
            },
        )

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cl.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def test_full_flow(self):
        self._commit("feat: 支持导出", day="2026-10-01")
        self._commit("chore: 调整脚本", day="2026-10-02")
        self._tag("v0.1.0")
        self._commit("fix: 修掉崩溃", day="2026-10-03")
        self._commit("feat(api)!: 换掉返回结构", "BREAKING CHANGE: data 字段改名 items", day="2026-10-04")

        code, out, _ = self._run("--no-links")
        self.assertEqual(code, 0)
        self.assertIn("## 未发布", out)
        self.assertIn("## [0.1.0] - 2026-10-02", out)
        self.assertIn("换掉返回结构", out)
        self.assertIn("data 字段改名 items", out)
        # 默认不显示 chore
        self.assertNotIn("调整脚本", out)

    def test_bump_from_history(self):
        self._commit("feat: a")
        self._tag("v1.2.3")
        self._commit("fix: b")
        code, out, _ = self._run("--bump")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "1.2.4")

    def test_bump_major_on_breaking(self):
        self._commit("feat: a")
        self._tag("v1.2.3")
        self._commit("feat!: b")
        _, out, _ = self._run("--bump")
        self.assertEqual(out.strip(), "2.0.0")

    def test_bump_prerelease(self):
        self._commit("feat: a")
        self._tag("v1.2.3")
        self._commit("feat: b")
        _, out, _ = self._run("--bump", "--pre", "rc")
        self.assertEqual(out.strip(), "1.3.0-rc.1")

    def test_slash_tag_not_mangled(self):
        # 回归：version/1.0.16 曾被渲染成 ersion/1.0.16
        self._commit("feat: a")
        self._tag("version/1.0.16")
        _, out, _ = self._run("--no-links")
        self.assertIn("[1.0.16]", out)
        self.assertNotIn("ersion/1.0.16", out)

    def test_unreleased_only(self):
        self._commit("feat: 老的")
        self._tag("v1.0.0")
        self._commit("feat: 新的")
        _, out, _ = self._run("--unreleased", "--no-links")
        self.assertIn("新的", out)
        self.assertNotIn("老的", out)

    def test_write_is_idempotent(self):
        self._commit("feat: 新功能")
        path = os.path.join(self.tmp, "CHANGELOG.md")
        self._run("--write", path)
        first = Path(path).read_text(encoding="utf-8")
        self._run("--write", path)
        second = Path(path).read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(second.count("## 未发布"), 1)

    def test_write_keeps_old_release(self):
        self._commit("feat: 老的")
        self._tag("v1.0.0")
        self._commit("feat: 新的")
        path = os.path.join(self.tmp, "CHANGELOG.md")
        self._run("--write", path)
        self._commit("fix: 更新的", day="2026-10-06")
        self._run("--write", path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("## [1.0.0]", text)
        self.assertIn("更新的", text)

    def test_write_orders_newest_first(self):
        # 回归：倒着插入段落时曾经把最老的版本顶到最前面
        self._commit("feat: 一")
        self._tag("v1.0.0")
        self._commit("feat: 二")
        self._tag("v1.1.0")
        self._commit("feat: 三")
        path = os.path.join(self.tmp, "CHANGELOG.md")
        self._run("--write", path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertLess(text.index("## 未发布"), text.index("## [1.1.0]"))
        self.assertLess(text.index("## [1.1.0]"), text.index("## [1.0.0]"))

    def test_strict_mode(self):
        self._commit("feat: 规范的")
        self._commit("随便写的一条")
        _, out, _ = self._run("--strict", "--no-links")
        self.assertIn("规范的", out)
        self.assertNotIn("随便写的一条", out)

    def test_all_flag_shows_chore(self):
        self._commit("chore: 杂事")
        _, out, _ = self._run("--all", "--no-links")
        self.assertIn("杂事", out)

    def test_hide_extra_type(self):
        self._commit("feat: 功能")
        self._commit("refactor: 重构")
        _, out, _ = self._run("--hide", "refactor", "--no-links")
        self.assertNotIn("重构", out)

    def test_since_flag(self):
        self._commit("feat: 第一条")
        self._tag("v1.0.0")
        self._commit("feat: 第二条")
        _, out, _ = self._run("--since", "v1.0.0", "--no-links")
        self.assertIn("第二条", out)
        self.assertNotIn("第一条", out)

    def test_rev_range_positional(self):
        self._commit("feat: 第一条")
        self._tag("v1.0.0")
        self._commit("feat: 第二条")
        _, out, _ = self._run("v1.0.0..HEAD", "--no-links")
        self.assertIn("第二条", out)

    def test_not_a_repo(self):
        bare = tempfile.mkdtemp(prefix="commitlog-bare-")
        self.addCleanup(shutil.rmtree, bare, ignore_errors=True)
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(bare)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cl.main([])
        finally:
            os.chdir(old)
        self.assertEqual(code, 1)
        self.assertIn("不是 git 仓库", err.getvalue())

    def test_empty_repo(self):
        code, _, err = self._run()
        self.assertEqual(code, 0)
        self.assertIn("没有提交", err)


if __name__ == "__main__":
    unittest.main()
