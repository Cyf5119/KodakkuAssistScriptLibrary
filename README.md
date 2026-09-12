# KodakkuAssistScriptLibrary

存放 [KodakkuAssist](https://github.com/Karlin-Z/KodakkuAssistScript) 各贡献者脚本仓库索引（`OnlineRepo.json`）的公共库。

每个贡献者拥有一个以自己 GitHub 用户名命名的文件夹，里面放自己的索引文件：

```
KodakkuAssistScriptLibrary/
├─ Karlin-Z/
│  └─ OnlineRepo.json
├─ ShoOtaku/
│  └─ OnlineRepo.json
└─ ...
```

## 如何贡献

1. Fork 本仓库。
2. 在**仓库根目录**新建一个以你的 GitHub 用户名命名的文件夹（例如登录名是 `Karlin-Z`，文件夹就叫 `Karlin-Z`；用户名不区分大小写，但建议完全一致）。
3. 把你的 `OnlineRepo.json` 放进去。
4. 向 `main` 分支提交 Pull Request。
5. 机器人（[PR Auto Review](.github/workflows/pr-auto-review.yml)）会自动审核：
   - **通过** → 自动 squash 合并，无需人工操作；
   - **不通过** → 在 PR 下留言列出全部问题，不会合并，修好后重新推送即可。

> 每个 PR 只能包含你自己文件夹里的改动。想改别人的文件、改 `README.md` 或 `.github/`，都会被拒绝。

## OnlineRepo.json 格式

顶层必须是**数组**，每个元素是一个脚本条目：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Name` | string | ✅ | 脚本名，不能为空 |
| `Guid` | string | ✅ | 脚本唯一 ID，建议使用标准 UUID |
| `Version` | string | ✅ | 1~4 段数字，如 `0.0.1`、`0.0.0.9` |
| `Author` | string | ✅ | 作者名 |
| `DownloadUrl` | string | ✅ | 脚本下载地址，允许空字符串；非空时必须是 `http(s)://` |
| `TerritoryIds` | int[] | ✅ | 地图 ID 数组，允许空数组 `[]` |
| `Repo` | string | ❌ | 会被插件覆盖，填什么都无效，建议留空 |
| `Note` | string | ❌ | 备注 |
| `UpdateInfo` | string | ❌ | 更新说明 |

以上字段对应插件源码里的 `KodakkuAssist.Script.OnlineScriptInfo`（`Interface/ScriptAttribute.cs`）。有几个容易踩的语义：

- `Version` 由插件的 `NuGetVersion` 解析，非法值会在脚本列表 UI 渲染时直接抛异常，所以必须是合法版本号（如 `0.0.1`、`0.0.0.9`、`1.0.0-beta`）。
- `TerritoryIds` 是 `HashSet<uint>`，取值 0~4294967295；写成负数或超范围会让**整个文件**解析失败，重复值会被自动去重。
- `Name` / `Author` 会被插件拼成下载后的保存文件名 `Name_Author.cs`（`ScriptManager.cs`），所以不能含 `／ \ : * ? " < > |` 等不能用于文件名的字符、控制字符、`..`，也不能是 `CON` / `NUL` 这类 Windows 保留名，否则可能写到缓存目录之外或直接失败。
- `Repo` 在插件读取时会被当前订阅地址覆盖（`ScriptManager.cs: info.Repo = repoUrl`），文件里填的内容不会被使用。
- C# 侧所有字段都有默认值，缺失时插件不会报错；但本库要求把上表「必填」字段写全，以保证索引可用。

示例：

```json
[
  {
    "Name": "M1s绘图",
    "Guid": "8010d865-7d6d-4c23-92e0-f4b0120e18ac",
    "Version": "0.0.0.9",
    "Author": "Karlin",
    "Repo": "",
    "DownloadUrl": "https://raw.githubusercontent.com/Karlin-Z/KodakkuAssistScript/main/07-DawnTrail/M1s.cs",
    "Note": "",
    "UpdateInfo": "",
    "TerritoryIds": [1226]
  }
]
```

## 自动审核规则

**阻塞合并的检查（必须全部通过）：**

1. 改动路径的第一层文件夹必须等于 PR 作者的 GitHub 用户名。
2. 只允许新增 / 修改 / 删除 `.json` 文件；重命名时，改名前后两个路径都必须满足上述两条。
3. 文件必须是合法 UTF-8 且是合法 JSON：顶层为数组，条目字段类型正确，`Version` 是合法 NuGet 版本号，`TerritoryIds` 全部是 0~4294967295 的整数，`Name` / `Author` 可安全用作文件名，同一文件内 `Guid` 不重复。
4. 不允许符号链接、子模块；单文件不超过 2 MB；单个 PR 不超过 100 个文件。

**只提醒、不阻塞的检查（会在评论里列出）：**

- `Guid` 不是标准 UUID（兼容历史数据，插件按字符串处理，可以识别但建议规范）；
- 出现了未定义的字段；
- `DownloadUrl` 没有使用 `https`；
- 同一文件内 `Name` 重复；
- `Note` / `UpdateInfo` 等可选字段被写成了数字 / 布尔值（会被转换成字符串）；
- 文件带 UTF-8 BOM（Windows 记事本常见）。

## 仓库维护者的一次性配置

自动审核依赖 `pull_request_target` + `GITHUB_TOKEN`，需要确认以下设置：

- **Settings → Actions → General → Workflow permissions** 选择 **Read and write permissions**。
  否则 `GITHUB_TOKEN` 没有合并权限，校验通过也无法合并。
- **Actions** 必须是启用状态。
- 工作流文件必须先存在于默认分支 `main`，`pull_request_target` 才会触发。
- **分支保护**：如果 `main` 开启了 “Require approvals” 或只允许指定用户合并，自动合并会失败。
  请关闭该限制，或把 `github-actions[bot]` 加入允许列表。
  另外不要把本工作流设为 Required status check——一旦脚本本身出问题，失败状态会连手动合并也一起挡住。
- 建议开启 **Settings → General → Automatically delete head branches**（工作流没有用 `--delete-branch`，因为它对 fork 分支常常无权限）。

## 本地校验（提 PR 前自检）

```bash
# 校验 JSON 是否符合规范
python .github/scripts/pr_review.py --schema-check Karlin-Z/OnlineRepo.json

# 校验路径规则：作者名 + 路径
python .github/scripts/pr_review.py --path-check Karlin-Z --path Karlin-Z/OnlineRepo.json

# 运行内置自测（不联网，验证脚本自身逻辑）
python .github/scripts/pr_review.py --selftest
```

## 可调规则

[`.github/scripts/pr_review_rules.json`](.github/scripts/pr_review_rules.json)：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `username_case_insensitive` | `true` | 文件夹名与用户名比较是否忽略大小写 |
| `allow_subfolders` | `true` | 是否允许在 `<用户名>/` 下再建子目录 |
| `required_filename` | `null` | 设为 `"OnlineRepo.json"` 可强制文件名 |
| `unknown_fields` | `"warn"` | 未知字段处理：`warn` / `error` / `ignore` |
| `max_files_per_pr` | `100` | 单个 PR 文件数上限 |
| `maintainers` | `[]` | 白名单登录名，可跳过「只能改自己文件夹」限制（**默认关闭**，开启后这些账号的 PR 可自动合并任意文件） |

## 安全说明

自动合并意味着「通过校验即进入主分支」，因此工作流做了以下约束：

- 审核脚本与配置**只从默认分支检出**，从不检出、也从不执行 PR 里的任何代码；PR 中的 `.json` 仅通过 GitHub API 以文本读取并解析。
- 审核脚本和规则配置位于 `.github/`，不在任何人的用户名文件夹内，所以 PR 无法修改它们（会被规则 1 拦下）。
- 拒绝符号链接与子模块，避免用链接把文件夹外的内容伪装成 `.json`。
- 合并时使用 `--match-head-commit` 锁定被审核的那个提交，防止「审核通过后又被推送新提交」的抢跑。
- 规则的唯一松口是 `maintainers` 白名单，默认空；请只填自己信任的账号。
