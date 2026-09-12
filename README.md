# Handbook AI Tutor（AI 智能学习助手）

把一份 **PDF 或 MP4 视频**交给它，它会自动帮你完成四件事：

1. **自动总结** —— 提炼全文摘要与大纲
2. **知识点梳理** —— 按章节拆出关键知识点
3. **出练习题** —— 生成选择题 + 翻译/写作/口语题，并给出解析
4. **AI 问答（Tutor Q&A）** —— 像老师一样回答你的问题，每条回答都附上原文出处（页码 / 时间戳）

全程本地运行，数据都保存在你自己的电脑上。

---

## 一、开始前：装两个软件（只装一次）

| 软件 | 说明 | 下载地址 |
| --- | --- | --- |
| Python | 版本 **3.11 或更高**，安装时**务必勾选 "Add Python to PATH"** | https://www.python.org/downloads/ |
| Node.js | 版本 **18 或更高**，选 **LTS 版**即可 | https://nodejs.org/ |

> 如果电脑上已经装过并能在命令行里运行 `python --version`、`node --version`，可跳过这一步。

---

## 二、第一次安装（只做一次）

1. 把项目文件夹放到你想长期存放的位置，例如 `D:\handbook-ai-tutor`。
2. 打开这个文件夹，在文件夹顶部**地址栏**里输入 `powershell` 然后回车，会弹出命令行窗口。
3. 把下面几行**依次**粘贴进去，每粘贴一行按一次回车：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e "./backend[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
cd frontend
npm install
npm run build
cd ..
```

看到命令全部跑完、没有红色报错，就说明安装成功了。以后不用再重复这一步。

---

## 三、打开网站（每次使用）

- **启动**：双击项目根目录下的 **`start.vbs`**
  - 不会弹出黑色命令行窗口，后台自动启动，等几秒后会自动打开浏览器进入 **http://localhost:3000**
- **关闭**：双击 **`stop.bat`**

> 首次进入页面时，点右上角「注册」创建一个账号即可（账号、资料都只保存在本机，单人使用）。

---

## 四、默认配置说明（无需改动）

- 数据库、对象存储使用**默认账号密码**（如 `minioadmin/minioadmin`、`tutor/tutor`）。
  因为这是单人下载使用、不涉及多用户隔离，所以**默认值直接保留即可，不需要修改**。
- 数据存放位置：账号 / 资料 / 答题记录在 `backend\tutor.db`，上传的原始文件在 `data\storage\`。

---

## 五、（可选）接入真实 AI

默认使用「离线模拟」模型，不需要任何密钥就能跑通完整流程。想接入真实 AI（效果更好）：

1. 用**记事本**打开项目根目录的 `.env` 文件
2. 找到 `DEEPSEEK_API_KEY=`，在等号后面填上你的 Key，保存
3. 先双击 `stop.bat`，再双击 `start.vbs` 重启即可

视频/音频课堂转写默认是离线 mock。要用 SiliconFlow 云端识别（法/德/中/英等自动检测，不必改 `start.vbs`）：

1. `.env` 里把 `STT_PROVIDER` 改成 `siliconflow`
2. 填写同一个 `SILICONFLOW_API_KEY`（和 LLM 共用，不用另开一把钥匙）
3. 双击 `stop.bat`，再双击 `start.vbs` 重启

> 密钥只保存在你本地的 `.env` 文件里，不会上传到任何地方（真实密钥已从项目里清空）。

---

## 六、常见问题

| 现象 | 解决办法 |
| --- | --- |
| 双击 `start.vbs` 打不开 | 确认已经完成「第一次安装」的 5 行命令，且 Python、Node 装好了 |
| 页面提示「无法连接后端服务」 | 双击 `stop.bat` 后再双击 `start.vbs` 重试 |
| 浏览器打开了但不是我的内容 | 换电脑 / 换目录后，需要重新执行「第一次安装」的命令 |

---

## 七、近期界面说明（原有功能都还在）

- **解析进度**：资料还在解析时，书架和原文页会走实时进度流（带登录态）。流断开时才退回原来的轮询。
- **文本来源**：摘要旁边会标明 OCR / 语音转写是真实识别还是离线模拟，避免把 mock 逐字稿当成课堂原文。
- **引用跳转**：Tutor 回答里的出处可以点，左侧原文预览会跳到对应页或时间戳。Quiz 解析里的「跳到原文」同样回到这份资料。
- **练习题**：重新生成会**另存一套新题**，旧题和作答记录还在。提交后可以对错题单独再练。口语题按你输入的文本对照参考答案批改，**不做语音识别评分**。
- **用量**：设置页和资料页可以看到每个任务、每份资料用了多少 token（不换算金额）。

启动 / 关闭方式不变：双击 `start.vbs` 打开 http://localhost:3000，双击 `stop.bat` 关闭。

---

## 开发者说明（可选阅读）

- 后端：[FastAPI](https://fastapi.tiangolo.com/)（`backend/`），前端：[Next.js 14](https://nextjs.org/)（`frontend/`）。
- 本仓库只保留了一种本地启动方式 `start.vbs`（静默启动，实际调用 `start.bat`）。
- 需要容器化部署时，可使用仓库内的 `docker-compose.yml`（对应 `infrastructure/` 目录）。
- 测试：`cd backend && python -m pytest -q`（默认使用 mock 模型，不调用真实 API）。