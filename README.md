# SakuraFRP Auto Check-in

话不多说，直接上图：

<img width="585" height="1269" alt="稳定签到成功截图" src="https://github.com/user-attachments/assets/34c9ce9a-95ad-49c5-81be-1aadfe69b91c" />

<img width="610" height="1315" alt="多轮失败自动重试" src="https://github.com/user-attachments/assets/e543e7f3-524a-466c-bdcf-6c7ffe9f713c" />

<img width="614" height="1324" alt="邮件通知内容展示" src="https://github.com/user-attachments/assets/7d6f766a-cfe0-497c-8a6d-ccd1ea0f4623" />

---

稳定版 SakuraFRP 自动签到脚本，支持：

- Playwright 自动化登录与签到
- 九宫格/滑块验证码处理
- 单次执行内多轮重试
- 成功/失败邮件通知
- 本地运行 + GitHub Actions 每日定时运行

原始项目参考链接：

- https://github.com/RyanStarFox/SakuraFRP_Auto_AI_check

## 1. 功能概览

当前版本重点在稳定性：

- 严格签到成功判定，避免误报
- 登录状态二次确认与自动补登录
- 18+ 弹窗自动处理
- 验证码失败自动重试
- 失败退出码正确返回，便于调度层重试

## 2. 项目结构

```text
.
├── main.py                  # 主程序
├── ai_service.py            # AI 能力封装
├── logger.py                # 日志记录器
├── requirements.txt         # Python 依赖
├── env.example              # 环境变量模板
├── run_checkin.sh           # 本地/服务器执行脚本（含外层重试）
├── generate_random_time.sh  # 本地随机时刻生成脚本
├── logs/                    # 运行日志
└── .github/workflows/       # GitHub Actions 工作流
```

## 3. 本地运行

### 3.1 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3.2 配置文件

1. 复制环境变量模板

```bash
cp env.example .env
```

2. 创建账号文件 `account.txt`

```text
第一行: 用户名
第二行: 密码
```

### 3.3 手动执行

```bash
source .venv/bin/activate
python main.py --both
```

## 4. 关键配置

在 `.env` 中常用配置如下：

- `ZHIPU_API_KEY`: 智谱 API Key
- `EMAIL_NOTIFY_ENABLED`: 是否启用邮件
- `EMAIL_SMTP_HOST/USER/PASSWORD`: SMTP 配置
- `EMAIL_TO`: 收件人
- `USE_STATE_CACHE`: 是否复用 state 登录态（推荐 `false`）
- `CAPTCHA_EXTRA_ROUND_ENABLED`: 验证码额外重试开关
- `CAPTCHA_EXTRA_ROUND_ATTEMPTS`: 验证码额外重试次数
- `CHECKIN_MAX_RETRIES`: 外层重试次数（建议 5）
- `CHECKIN_RETRY_INTERVAL_SECONDS`: 外层重试间隔秒数（建议 30）

## 5. GitHub Actions 部署

仓库内已提供工作流：

- 文件: `.github/workflows/checkin.yml`
- 触发方式:
  - 每天定时执行
  - 手动触发（workflow_dispatch）

### 5.1 定时说明

GitHub Actions 使用 UTC 时区。

- 目标: 每天北京时间 12:00
- 对应 cron: `0 4 * * *`

### 5.2 需要配置的 Secrets

在 GitHub 仓库 `Settings -> Secrets and variables -> Actions` 中添加：

- `SAKURA_USERNAME`
- `SAKURA_PASSWORD`
- `ZHIPU_API_KEY`
- `EMAIL_SMTP_USER`
- `EMAIL_SMTP_PASSWORD`
- `EMAIL_TO`
- `HTTP_PROXY` (可选)

工作流会基于这些 Secrets 动态生成运行时的 `.env` 和 `account.txt`。

### 5.3 首次验证

1. 打开 Actions 页面
2. 手动运行 `SakuraFRP Daily Check-in`
3. 检查 Job 日志
4. 下载 artifacts 查看 `logs/` 与截图
5. 确认邮件通知是否正常

## 6. 安全建议

- 不要把 `.env`、`account.txt`、`state.json` 提交到 Git
- 不要在 Issue/日志中粘贴明文密钥
- 建议定期轮换 API Key 与 SMTP 授权码

## 7. 故障排查

### 7.1 验证码连续失败

- 增大 `CHECKIN_MAX_RETRIES`
- 增大 `CAPTCHA_EXTRA_ROUND_ATTEMPTS`
- 检查网络代理连通性

### 7.2 邮件未收到

- 检查 SMTP 授权码是否有效
- 检查邮箱垃圾箱
- 查看日志中 `邮件发送失败` 报错

### 7.3 Actions 执行失败

- 检查 Secrets 是否配置完整
- 检查 Playwright 依赖安装日志
- 下载 artifacts 查看 `logs/checkin_YYYY-MM-DD.log`

## 8. 免责声明

本项目仅用于个人学习与自动化实践。请遵守目标网站服务条款与当地法律法规。
