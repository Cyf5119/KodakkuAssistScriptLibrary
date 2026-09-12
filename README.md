# KodakkuAssistScriptLibrary

[KodakkuAssist](https://github.com/Karlin-Z/KodakkuAssistScript) 脚本的公共聚合库。

贡献者直接提交 `.cs` 脚本文件，CI 自动从每个文件的 `[ScriptType(...)]` 特性里提取信息，
生成一份合并后的索引 `OnlineRepo.json`。使用者只要把这个索引填进插件的 `OnlineRepo`
就能订阅全库脚本。

```
KodakkuAssistScriptLibrary/
├─ OnlineRepo.json             ← 自动生成的总索引，请勿手动修改
├─ SimpleScript.cs             ← 示例模板，不属于任何人，不会进入索引
├─ .github/
│  └─ index/guid-map.json      ← 自动生成的 guid 登记表，供 PR 查重，请勿手动修改
├─ Karlin-Z/                   ← 以 GitHub 用户名命名的贡献者文件夹
│  ├─ M1s.cs
│  └─ 高难/                     ← 子目录随便建，任意深度的 .cs 都会被收录
│     └─ M2s.cs
└─ ...
```

**贡献者文件夹下的所有子目录都会被递归扫描**，深度不限（`Karlin-Z/a/b/c/x.cs` 一样收录），
所以你可以按副本、版本或任意方式组织自己的文件夹。

`SimpleScript.cs` 是官方示例脚本，可以作为写脚本的起点。**只有第一层文件夹里的 `.cs`
才会被收录**，所以根目录的它不会被索引——那只是模板，不是要分发的脚本。
如果你想把示例 / 模板放进某个目录（例如 `examples/`），记得把该目录名加进
[`pr_review_rules.json`](.github/scripts/pr_review_rules.json) 的 `ignore_dirs`，
否则除了 `.` / `_` 开头的目录，第一层目录都会被当作贡献者文件夹扫描并发布。

复制示例时**务必改掉 `guid`**。插件把相同 `guid` 视为同一个脚本，沿用示例的 guid
会在 PR 审核阶段被直接拒绝（详情见下面的审核规则）。

## 如何贡献

1. Fork 本仓库。
2. 在**仓库根目录**新建一个以你的 GitHub 用户名命名的文件夹（例如登录名是 `Karlin-Z`，文件夹就叫 `Karlin-Z`；用户名不区分大小写，但建议完全一致）。
3. 把你的 `.cs` 脚本放进去。**一个 `.cs` 只能包含一个脚本**，且必须包含且只包含一处 `[ScriptType(...)]`。
4. 向 `main` 分支提交 Pull Request。
5. 机器人（[PR Auto Review](.github/workflows/pr-auto-review.yml)）自动审核：
   - **通过** → 自动 squash 合并，并通过 [Build and Deploy Pages](.github/workflows/pages.yml) 重建索引；
   - **不通过** → 在 PR 下留言列出问题，不会合并，改好重新推送即可。

> 每个 PR 只能动你自己文件夹里的 `.cs`。改别人的文件夹、改 `README.md`、改 `.github/`，
> 或者直接改根目录的 `OnlineRepo.json`，都会被拒绝。

## ScriptType 参数

索引里的每个字段都来自脚本里的特性，例如：

```csharp
[ScriptType(name: "M1s绘图", territorys: [1226], guid: "8010d865-7d6d-4c23-92e0-f4b0120e18ac",
            version: "0.0.0.9", author: "Karlin")]
public class M1s { }
```

| 参数 | 类型 | 是否必填 | 说明 |
| --- | --- | --- | --- |
| `guid` | string | ✅ | 脚本唯一标识。插件把相同 `guid` 视为同一个脚本，务必不要与别人重复 |
| `name` | string | 建议 | 脚本名。不写会使用插件默认的 `Default Script` |
| `territorys` | uint[] | 建议 | 生效地图 ID，例如 `[1226]`；空数组表示不按地图过滤 |
| `version` | string | 建议 | 版本号。插件用 `NuGetVersion` 解析，写 `0.0.1`、`0.0.0.9`、`1.0.0-beta` 都可以 |
| `author` | string | 否 | 不写、留空或写成 `Unknown` 时，自动使用你的**文件夹名**（即 GitHub 用户名） |
| `note` | string | 否 | 备注 |
| `updateInfo` | string | 否 | 更新说明 |

参数可以用**具名实参且顺序任意**，也可以按构造函数顺序写成位置参数。
特性里可以使用本文件内的 `const`，长文本常见写法：

```csharp
[ScriptType(name: "绝凯夫卡", territorys: [1363], guid: "...", version: "0.0.0.4",
            author: "Karlin", updateInfo: updateInfoStr)]
public class 绝凯夫卡
{
    const string updateInfoStr = """
        精修  P1
        增加分摊击退指示
        """;
}
```

`Repo` 和 `DownloadUrl` 不需要你写：`DownloadUrl` 会自动填成本仓库该 `.cs` 文件的
raw 直链，`Repo` 会被插件在订阅时覆盖。

## 自动审核规则

**阻塞合并的检查（必须全部通过）：**

1. 改动路径的第一层文件夹必须等于 PR 作者的 GitHub 用户名。
2. 只允许新增 / 修改 / 删除 `.cs` 文件；重命名时，改名前后两个路径都必须满足上述两条。
3. 每个 `.cs` 必须包含**恰好一处** `[ScriptType(...)]`（或 `[ScriptTypeAttribute(...)]`）：
   - 一处都没有，或出现两处及以上 → 拒绝（注释里、字符串里的写法不计入）；
   - `guid` 必须是**非空字符串**；
   - `version` 必须是合法版本号；
   - `territorys` 必须是 0~4294967295 的整数数组字面量；
   - `name` / `author` 必须能安全用作文件名（插件用 `Name_Author.cs` 保存下载的脚本），
     因此不能含 `/ \ : * ? " < > |`、控制字符、`..`，也不能是 `CON` / `NUL` 这类 Windows 保留名。
4. `guid` 不能与已有脚本冲突。审核时会读取仓库里已提交的 `.github/index/guid-map.json`
   与本次提交的 guid 比对：
   - guid 已被**别人的**脚本占用 → 拒绝；
   - guid 已被**你自己文件夹里的另一个文件**占用 → 拒绝；
   - 同一个 PR 里两个文件用了同一个 guid → 拒绝；
   - 重命名 / 移动自己的文件（guid 不变）→ 放行，不算冲突。
5. 文件必须是合法 UTF-8；不允许符号链接、子模块；单文件不超过 2 MB；单个 PR 不超过 100 个文件。

**只提醒、不阻塞的检查（会出现在评论里）：**

- 没写 `name` / `version` / `territorys`，将使用插件默认值；
- 没写 `author` 或写成 `Unknown`，改用文件夹名；
- `guid` 不是标准 UUID 格式（兼容历史脚本，插件按字符串处理仍可用）；
- `note` / `updateInfo` 引用了本文件里找不到的 `const`，将按空字符串处理。

## 索引与 GitHub Pages

合并后的索引有两个等价入口：

| 地址 | 说明 |
| --- | --- |
| `https://raw.githubusercontent.com/<owner>/<repo>/main/OnlineRepo.json` | 仓库根目录的总索引，不依赖 Pages |
| `https://<owner>.github.io/<repo>/index.json` | Pages 上的同一份索引 |

合并由 [`.github/scripts/merge_repos.py`](.github/scripts/merge_repos.py) 完成：

- 只扫描第一层里非 `.` / `_` 开头的目录（即贡献者文件夹），进入文件夹后**递归全部子目录**，
  深度不限；被 `ignore_dirs` 点名的目录名在任何层级都会被跳过；
- 每个文件用**与 PR 审核完全相同**的规则校验（复用 `pr_review.py`，避免两处规则漂移），不合规的跳过并列入构建报告；
- `DownloadUrl` 按 `<仓库>/<分支>/<路径>` 自动生成，中文与空格路径会做百分号编码；
- 按 `Guid`（忽略大小写）去重，先出现的生效，冲突在报告中列出；
- 同时生成 `.github/index/guid-map.json`（guid → 源文件），供下一轮 PR 审核查重；
- 输出顺序固定（先按文件夹名、再按路径），内容不随时间变化，所以「重新生成」在没有改动时不会产生提交。

### 触发方式

`push` 到 `main` 和 `workflow_dispatch` 两种都会触发，缺一不可：自动合并用的是 `GITHUB_TOKEN`，
而 GitHub 规定 `GITHUB_TOKEN` 触发的 `push` 不会再触发新的 workflow（`workflow_dispatch` /
`repository_dispatch` 是仅有的两个例外）。所以自动合并成功后，`pr-auto-review.yml` 会用
`gh workflow run` 显式派发一次，否则索引永远停在旧版本。

想让**站点根路径**直接返回 JSON（而不是给浏览器看说明页），在 `pages.yml` 的生成命令上加
`--root-json-mode`。插件用 `GetStringAsync` 读取，不检查 `Content-Type`，所以根路径返回 JSON
也能正常订阅。

## 仓库维护者的一次性配置

自动审核依赖 `pull_request_target` + `GITHUB_TOKEN`，需要确认以下设置：

- **Settings → Actions → General → Workflow permissions** 选择 **Read and write permissions**。
  否则 `GITHUB_TOKEN` 没有合并权限，校验通过也无法合并，`publish-root` 也无法提交索引。
- **Actions** 必须是启用状态。
- 工作流文件必须先存在于默认分支 `main`，`pull_request_target` 才会触发。
- **分支保护**：如果 `main` 开启了 “Require approvals”、“Require a pull request before merging”
  或限制推送，自动合并和 `publish-root` 的提交都会失败。请关闭这些限制，或把
  `github-actions[bot]` 加入允许列表。
  另外不要把本工作流设为 Required status check——一旦脚本本身出问题，失败状态会连手动合并一起挡住。
- **Pages**：**Settings → Pages → Source** 选 **GitHub Actions**。
  如果 `github-pages` 环境配了 required reviewers，部署会一直卡住等待人工批准，需要去掉。
- 不要点 Pages 设置页面里的 “Jekyll” / “Static HTML” 模板卡片——那会额外生成一个部署
  workflow，和 `pages.yml` 抢着发布。
- 建议开启 **Settings → General → Automatically delete head branches**。

## 本地校验（提 PR 前自检）

```bash
# 校验 .cs（--owner 不传时会尝试从仓库目录结构推断文件夹名）
python .github/scripts/pr_review.py --owner Karlin-Z --check-cs Karlin-Z/M1s.cs

# 校验路径规则
python .github/scripts/pr_review.py --path-check Karlin-Z --path Karlin-Z/M1s.cs

# 校验生成的索引
python .github/scripts/pr_review.py --check-json OnlineRepo.json

# 本地生成索引预览（输出到 _site/）
python .github/scripts/merge_repos.py --out _site

# 内置自测（不联网，验证脚本自身逻辑）
python .github/scripts/pr_review.py --selftest
python .github/scripts/merge_repos.py --selftest
```

## 可调规则

[`.github/scripts/pr_review_rules.json`](.github/scripts/pr_review_rules.json)：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `username_case_insensitive` | `true` | 文件夹名与用户名比较是否忽略大小写 |
| `allow_subfolders` | `true` | 是否允许在 `<用户名>/` 下再建子目录 |
| `allowed_extensions` | `[".cs"]` | 贡献者文件夹里允许的文件扩展名 |
| `max_files_per_pr` | `100` | 单个 PR 文件数上限 |
| `ignore_dirs` | `[]` | 不参与索引的目录名，**任意层级**匹配。例如加 `["examples"]` 后，`examples/` 不会被当成贡献者文件夹、`alice/examples/x.cs` 也会被跳过 |
| `maintainers` | `[]` | 白名单登录名，可跳过「只能改自己文件夹」限制（**默认关闭**） |
| `unknown_fields` | `"warn"` | 生成结果出现未知字段时：`warn` / `error` / `ignore` |

## 安全说明

**这个仓库托管的是会被插件下载并编译执行的 C# 代码，不再只是数据。** 也就是说，
合并进来的任何 PR 都会在订阅者机器上运行。当前自动合并是**全自动**的：任何人只要通过
上面那些格式检查就会被合并并发布，格式检查**不审查代码行为**。如果这个风险不可接受，
可以改成「新贡献者需人工合并」或「全部人工合并」，改动量不大。

流程上已有的约束：

- 审核脚本与配置**只从默认分支检出**，从不检出、也从不执行 PR 里的任何代码；`.cs` 只当文本解析，从不编译。
- 审核脚本和规则配置位于 `.github/`，不在任何人的用户名文件夹内，所以 PR 无法修改它们（会被规则 1 拦下）。
- 拒绝符号链接与子模块，避免用链接把文件夹外的内容伪装成 `.cs`。
- 合并时使用 `--match-head-commit` 锁定被审核的那个提交，防止「审核通过后又被推送新提交」的抢跑。
- 规则里唯一的松口是 `maintainers` 白名单，默认空；请只填自己信任的账号。
