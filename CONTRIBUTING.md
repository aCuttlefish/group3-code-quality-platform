# 小组协作：先跑通，再完善

目标是下周完成演示。每人负责一个功能，不要求互相评审代码，不强制走 PR。自动测试只帮助发现问题，不作为合并门槛。

## 第一次运行

```powershell
git clone https://github.com/aCuttlefish/group3-code-quality-platform.git
cd group3-code-quality-platform
python app.py
```

打开 http://127.0.0.1:8765。Python 需要 3.10+，目前无需安装第三方包。各自在用户环境变量或本机 `.env` 配置 `DEEPSEEK_API_KEY`，不要提交密钥。未配置时可使用演示模式。

## 平时怎么提交

每个人在自己的功能分支开发。以下是陈宏伟的示例，其他人替换成自己的分支名（见分工文档）：

```powershell
git fetch origin
git switch feat/fr01-problem-localization
git merge origin/main
# 修改代码，运行 python app.py 检查自己的功能
git add <本次修改的文件>
git commit -m "完成问题定位基础功能"
git push -u origin feat/fr01-problem-localization
```

`<本次修改的文件>` 是占位文字，替换为实际路径。功能能跑后可以自己合并，不需要别人评审：

```powershell
git switch main
git pull --ff-only origin main
git merge feat/fr01-problem-localization
git push origin main
```

小修改也可以直接提交 `main`，先拉取最新代码。若推送提示远程已更新，先拉取并解决冲突，不要强制推送。改 `app.py` 或同一页面文件前在群里说一声，避免同时修改同一段。

合并后启动页面跑一遍自己的功能；有时间执行 `python -m unittest discover -s tests -v`。GitHub 自动测试失败可以作为排错线索，不阻止合并。

仓库公开但只有受邀成员能直接推送。还没有写入权限时把 GitHub 用户名给仓库管理员添加；在此之前可以克隆运行或 Fork。不要提交 `.env`、运行数据、缓存和压缩包。
