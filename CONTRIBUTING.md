# Git 协作指南

## 开始开发

需要 Git 和 Python 3.10+。目前运行不需要第三方 Python 包。每个人使用自己的 DeepSeek 密钥，放在用户环境变量或本机 `.env`，不要放进代码、Issue、截图或 PR。

```powershell
git clone https://github.com/aCuttlefish/group3-code-quality-platform.git
cd group3-code-quality-platform
python -m unittest discover -s tests -v
python app.py
```

浏览器访问 http://127.0.0.1:8765。未配置密钥时可用页面演示模式验证流程。

## 一人一个功能

| 姓名 | 功能分支 | 主责 | 默认交叉评审人 |
| --- | --- | --- | --- |
| 陈宏伟 | `feat/fr01-problem-localization` | FR-01 | 彭启诚 |
| 彭启诚 | `feat/fr02-quality-assessment` | FR-02 | 敖德太 |
| 敖德太 | `feat/fr03-improvement-suggestions` | FR-03 | 薛奕辰 |
| 薛奕辰 | `feat/fr04-test-generation` | FR-04 | 陈宏伟 |

这是后续完整项目的分工，不代表之前两小时原型严格按上述分工完成。GitHub 账号未登记前以姓名约定责任；仓库写入权限需要管理员邀请后才能获得。公开可访问不等于可以直接推送，没有写入权限时请 Fork 后提交 PR。

有写入权限时，以 FR-01 为例：

```powershell
git fetch origin
git switch --track origin/feat/fr01-problem-localization
# 已有本地分支时使用 git switch feat/fr01-problem-localization
git merge origin/main
# 完成修改并运行测试，再显式选择要提交的文件
git add fr01 tests docs
git commit -m "feat(fr01): add rule-based issue localization"
git push -u origin feat/fr01-problem-localization
```

示例中的 `fr01` 目录由 FR-01 开发者创建；不要在该目录尚不存在时照抄 `git add`。新建 PR 时目标选择 `main`。一次 PR 完成一个可验收的小改动，写明需求、实现、测试和限制；提交前同步 `main`。保留远程功能分支，合并后再次同步 `main`，不要强制推送共享分支。

## 合并约定

1. `main` 保持可启动；功能代码经 PR 进入，由另一位同学评审。
2. 自动测试通过之后合并；真实 AI 验收由功能负责人本地执行并附脱敏记录，CI 不使用真实密钥。
3. 优先把新增逻辑放在 `fr01/`、`fr02/`、`fr03/`、现有 `fr04/`；对应测试放在 `tests/test_fr01_*.py` 等文件中。
4. `app.py`、`static/index.html`、`static/app.js`、`static/style.css` 和接口文档是共享文件，修改前在对应 Issue 标明范围。同一时间只合并一个共享入口变更；接口变更先更新文档并通知受影响负责人在该 PR 评审。
5. 陈宏伟负责组织联调与版本发布，彭启诚负责汇总验收判据；各模块负责人仍对自己模块的测试和修复负责。

分支保护是否生效以 GitHub 设置为准；文档约定本身不能阻止直接推送。

## 验证 AI 输出

AI 输出先经过结构、枚举、范围、行号和版本校验，再与人工制定的固定样例对照。断言的预期来自需求或独立计算，不能从被测函数的实际输出反推，也不能因为失败而改成实际值。至少保留正确样例、已知缺陷样例、需求变化样例和无效响应样例。

FR-04 用 99 元免邮边界演示：S1 要求 `>=99`，含 `>99` 缺陷的实现必须在 99 失败；修复后通过；S2 改成 `>99` 后重新生成，预期运费为 10。验证生成用例是否真的触达边界和预期是否正确；全部通过不能证明不存在缺陷。演示模式只证明执行流程，不能作为真实模型质量证据。

## 提交前检查

```powershell
python -m unittest discover -s tests -v
# 修改前端时，如本机安装了 Node：
node --check static/app.js
git diff --check
git diff --cached
```

不提交 `.env`、数据目录、模型原始业务输入、课程报告、压缩包或缓存。新增依赖要在 PR 解释用途并补安装说明。当前执行器适用于本机教学，不把服务监听地址改成公网地址。
