# Issue tracker: GitHub

本项目的规格和任务使用 GitHub Issues，仓库为 `lzh-zzz/AI-Email-FollowUp`。

## 操作约定

- 使用 `gh` CLI；显式指定仓库 `--repo lzh-zzz/AI-Email-FollowUp`，避免把任务发布到其他仓库。
- 发布或修改前读取相关 Issue 的正文、评论和标签，检查是否已有同一项工作，优先更新已有记录。
- 多行正文先保存为 UTF-8 文本，再使用 `--body-file`；保留真实换行，不把正文拼接进 shell 命令。
- 标签角色按 `triage-labels.md` 映射；规格尚有未决事项时不标记为 `ready-for-agent`。
- 若缺少 CLI、未登录或权限不足，明确报告实际限制；本地文档保存成功不代表 GitHub 发布成功。

## 常用操作

- 创建：`gh issue create --repo lzh-zzz/AI-Email-FollowUp --title "标题" --body-file <正文文件> --label <标签>`。
- 读取：`gh issue view <编号> --repo lzh-zzz/AI-Email-FollowUp --comments`；需要完整元数据时另取正文、标签和评论的 JSON。
- 列举：`gh issue list --repo lzh-zzz/AI-Email-FollowUp --state open --json number,title,body,labels,comments`；按需筛选标签和状态。
- 修改正文：`gh issue edit <编号> --repo lzh-zzz/AI-Email-FollowUp --body-file <正文文件>`。
- 评论：`gh issue comment <编号> --repo lzh-zzz/AI-Email-FollowUp --body-file <评论文件>`。
- 添加或移除标签：`gh issue edit <编号> --repo lzh-zzz/AI-Email-FollowUp --add-label <标签>` 或 `--remove-label <标签>`。
- 关闭：只有任务真实完成且有依据时使用 `gh issue close <编号> --repo lzh-zzz/AI-Email-FollowUp`。

## 技能中的发布与读取

- “publish to the issue tracker”表示创建或更新 GitHub Issue，不以本地文件替代远程发布结果。
- “fetch the relevant ticket”表示读取对应 Issue 的完整正文、评论和标签。
- 本地 `SPEC.md` 是当前规格草案；经确认发布后，本地规格与其 GitHub Issue 保持一致，记录真实 Issue 链接。
- 拆票时每个可独立验证的完整业务切片对应一个 Issue；先创建阻塞任务，再创建依赖它的任务。
- 优先使用 GitHub 原生阻塞关系，引用 Issue 的数据库 ID；若仓库不支持，以正文中的 `Blocked by: #编号` 明确记录。
- 只执行所有阻塞项均已完成的任务；拆票操作不关闭或修改父 Issue。

## Pull requests as a triage surface

**PRs as a request surface: no.**

PR 默认不作为需求受理入口。GitHub Issue 和 PR 共享编号空间；引用对象不明确时核实其类型，不把 PR 当作 Issue 操作。
