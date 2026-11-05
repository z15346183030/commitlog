# commitlog

把 git 提交历史变成 CHANGELOG。零配置，单文件，只用标准库。

```bash
python commitlog.py                  # 整个历史，打到屏幕
python commitlog.py --unreleased     # 只看上一个 tag 之后
python commitlog.py --bump           # 只打印下一个版本号
python commitlog.py --write CHANGELOG.md   # 写进文件，重跑不会重复
```

Python 3.9 以上，没有依赖，没有配置文件，没有子命令。

---

## 它解决的是什么问题

`git log` 是给写代码的人看的，CHANGELOG 是给用代码的人看的。两者之间差三件事：

1. **过滤**——`chore: 调整 CI` 对使用者毫无意义，不该出现在发布说明里。
2. **归组**——新功能、修复、破坏性变更，得分开站队。
3. **版本**——这次该发 1.3.0 还是 2.0.0，不该靠人回忆。

现有的生成器把这三件事做成了「配置工程」：一份 TOML，几十条正则，外加一个模板语言。你想让 `docs` 提交不出现，得写 `{ message = "^doc", skip = true }`；你想让 `feat` 单独成章，得写 `{ message = "^feat", group = "新功能" }`。

`commitlog` 的取舍是：**Conventional Commits 已经规定了类型，那就不该再让你用正则描述一遍。**

`feat`、`fix`、`perf` 这些前缀本身就是分类。工具认识它们，默认就知道怎么分组、哪些该藏、版本该怎么进。你什么都不用写。

---

## 装

一个文件，复制走就能用：

```bash
curl -O https://raw.githubusercontent.com/z15346183030/commitlog/main/commitlog.py
python commitlog.py --help
```

想当命令用，加个别名：

```bash
# ~/.bashrc 或 ~/.zshrc
alias commitlog='python /path/to/commitlog.py'
```

---

## 默认长什么样

在任意 git 仓库里直接跑，不加任何参数：

```
$ python commitlog.py
# Changelog

## 未发布

### 破坏性变更

- 返回结构从 list 改成 dict —— 字段 data 改名为 items

### 新功能

- 返回结构从 list 改成 dict ([a9f547a](https://github.com/o/r/commit/a9f547a3063d6f4c66907dc210f2fbbe61b668fb))

### 问题修复

- **api**: 分页越界返回空列表 ([#42](https://github.com/o/r/pull/42)) ([71b9d2c](https://github.com/o/r/commit/71b9d2cb749eb19159b8d3efd28573515a19ef1d))

## [0.2.0] - 2026-09-12

### 新功能

- **api**: 增加分页参数 ([d29225d](https://github.com/o/r/commit/d29225dedfff9e123e97a7652610c8e24a20d8b1))

### 问题修复

- 时间戳时区错乱 ([83b5338](https://github.com/o/r/commit/83b5338d53f456bfeeb39f109edc4055bdb59a39))

### 性能优化

- 导入速度提升 3 倍 ([2a0d844](https://github.com/o/r/commit/2a0d844a694935bb417bbf19f41507fe139cb8bd))

## [0.1.0] - 2026-09-04

### 新功能

- 支持从 CSV 导入 ([0d01c19](https://github.com/o/r/commit/0d01c191c06408ea38f06eeaad0a102699750e60))

### 问题修复

- **cli**: 修掉空参数崩溃 ([da63d2a](https://github.com/o/r/commit/da63d2a686cb423cb5a203efdcb8b8de6f7ae311))
```

链接是从 `git remote get-url origin` 推出来的，不用配。没有 remote 就只写短 hash。

### 类型到章节的映射

| 提交类型 | 章节 | 默认显示 |
|---|---|---|
| `feat` | 新功能 | ✅ |
| `fix` | 问题修复 | ✅ |
| `perf` | 性能优化 | ✅ |
| `refactor` | 重构 | ✅ |
| `revert` | 回滚 | ✅ |
| `docs` | 文档 | ❌ |
| `build` | 构建 | ❌ |
| `ci` | 持续集成 | ❌ |
| `test` | 测试 | ❌ |
| `style` | 代码风格 | ❌ |
| `chore` | 杂项 | ❌ |
| 其他 / 不合规范 | 其他变更 | ✅ |

想全都要：`--all`。想再藏几个：`--hide refactor,revert`。只留规范的：`--strict`。

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `commitlog` | 整个历史 |
| `commitlog --unreleased` | 上一个 tag 之后的部分 |
| `commitlog v1.2.0..HEAD` | 指定区间 |
| `commitlog --since v1.2.0` | 同上，写法更顺 |
| `commitlog --bump` | 打印下一个版本号，如 `1.3.0` |
| `commitlog --bump --pre rc` | 打印 `1.3.0-rc.1` |
| `commitlog --write CHANGELOG.md` | 写文件，同版本段落替换而非追加 |
| `commitlog --all` | 连 chore / docs 一起列 |
| `commitlog --no-links` | 不生成任何链接 |
| `commitlog --repo https://github.com/a/b` | 手动指定链接前缀 |

---

## 版本号怎么算

`--bump` 只看**上一个 tag 之后**的提交。

| 情况 | 结果 | 例子 |
|---|---|---|
| 有 `!` 或 `BREAKING CHANGE:` | 主版本 +1 | `1.2.3` → `2.0.0` |
| 主版本是 0 且有破坏性变更 | 次版本 +1 | `0.4.1` → `0.5.0` |
| 有 `feat` | 次版本 +1 | `1.2.3` → `1.3.0` |
| 只有 `fix` / `perf` / 其他 | 修订号 +1 | `1.2.3` → `1.2.4` |
| 一个 tag 都没有 | 从 `0.1.0` 开始 | → `0.1.0` |
| 上一个 tag 是预发布 | 转正，不再进版本 | `1.3.0-rc.2` → `1.3.0` |
| `--pre beta` 且上一个是 `beta.1` | 同序列 +1 | `1.3.0-beta.1` → `1.3.0-beta.2` |

主版本为 0 时破坏性变更只进次版本，是因为 0.x 还没定型——这条约定来自 semver 第 4 条，也是多数 0.x 项目的实际做法。

放进 CI 就这么写：

```bash
NEXT=$(python commitlog.py --bump)
git tag "v$NEXT"
python commitlog.py --write CHANGELOG.md
```

---

## 重复跑不会写出两份

`--write` 按版本号认段落：同一版本已经存在就替换，不存在才插到最上面。

```bash
$ python commitlog.py --write CHANGELOG.md
已写入 CHANGELOG.md（3 个版本段落）
$ python commitlog.py --write CHANGELOG.md
已写入 CHANGELOG.md（3 个版本段落）
$ md5sum CHANGELOG.md && python commitlog.py --write CHANGELOG.md && md5sum CHANGELOG.md
4cbdba8211505d58f71fc283d0757ca5 *CHANGELOG.md
4cbdba8211505d58f71fc283d0757ca5 *CHANGELOG.md
```

文件开头是你自己写的东西（项目简介、许可证说明）不会被碰——只有 `## ` 开头的段落归工具管。

---

## 几个刻意的选择

**标签名里的版本号靠「找」不靠「削」。** `v1.2.3`、`version/1.2.3`、`release-1.2.3`、`pkg-core@2.0.1` 都能认出来，因为它们末尾都有一个完整的 `x.y.z`。反过来，标签就叫 `version` 或 `nightly` 的，认不出来就当普通标签忽略，不会硬凑一个版本号出来。

**默认不显示合并提交。** `Merge pull request #12 from ...` 对读者没有信息量，而且绝大多数解析不出类型。需要就 `--merges`。

**同一章节内按时间正序。** 版本之间从新到旧，版本内部从旧到新——这样读一个版本像读一条时间线。

**解析不了的提交不静默丢弃。** 它们进「其他变更」。宁可多一行噪音，也不要让一次真实的改动凭空消失。真嫌吵，`--strict`。

---

## 测试

```bash
python -m unittest discover -s tests
```

101 个用例。核心逻辑（版本号解析、类型解析、归组、版本推算、渲染、幂等写入）全是纯函数，输入输出都是字符串，不需要建仓库；最后一组用例会在临时目录里建真仓库，跑完整链路。

---

## 默认值优先

CHANGELOG 工具的复杂度，往往来自「让用户配置一切」：模板语言、提交分类规则、远程仓库集成、
贡献者统计、输出格式。配置越多，第一次生成越慢，团队里越没人愿意维护。

这里只做一件事：**默认值足够好，好到不需要写配置。** 常规提交按类型归组，版本号按
语义化规则推算，没有模板语法，没有必填项。需要更多格式时，JSON 输出可以交给下游脚本。

MIT 许可证。
