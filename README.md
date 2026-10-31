# DMIT Premium 补货响铃提醒

GitHub Actions 在云端检查 DMIT，iPhone 通过 Bark 响铃。不需要 VPS，电脑可以关机。

**当前交付状态：代码已上传；需要完成云端 check 和手机 test 两项实测，均通过后才算启用。首次云端检查遇到 HTTP 403，正在验证正常浏览器加载。**

## 已设置的筛选条件

- 美国西部：目前官网主要是洛杉矶 LAX，同时识别未来可能出现的 SJC、SEA、SFO 标识。
- 只关注 Premium / Pro，排除 Eyeball / EB 和 Tier 1 / T1。
- 月付实际美元金额 **<20**，或年付实际美元金额 **<100**，满足其中一项即可。
- 只读基础订阅计费选项；不把月费乘以 12 当成年费，不使用未经验证的优惠码。
- 每小时第 7 和 37 分钟安排检查，避开整点拥堵。
- 发现符合条件且可进入正确商品配置页的产品，推送标题：**DMIT服务器已经开放**。
- 同一价格、同一次有货期间只提醒一次；确认缺货后再次有货，或符合预算的价格变化时重新提醒。
- 连续三次检查失败发送一次普通故障通知；成功恢复再发一次恢复通知。

## 第一步：iPhone 安装 Bark

1. 在 App Store 安装 [Bark](https://apps.apple.com/app/bark-custom-notifications/id1403753865)。
2. 允许通知，并在 Bark 内或 iPhone 的“设置 → 通知 → Bark”允许“重要警告”；也开启声音、锁屏通知。
3. Bark 首页会显示类似 `https://api.day.app/AbCdEf123456/推送内容` 的个人推送地址。
4. 复制这条个人链接，下一步直接粘贴到 GitHub Secret；程序会自动提取设备密钥。只填写 `AbCdEf123456` 这一段也可以。不要把真实密钥或链接写到代码、README、Issue 或仓库文件里。

程序使用 `level=critical`、`sound=alarm`、`volume=10`、`call=1`，请求最高音量的重要警告，并让铃声循环约 30 秒。它是推送铃声，不是“时钟”App 的系统闹钟，也不会无限响到你手动关闭。能否绕过静音和专注模式，必须在这台 iPhone 上实际测试。

## 第二步：上传到 GitHub

新建一个只放本项目代码的仓库，例如 `dmit-stock-monitor`。推荐**公开仓库配标准 Ubuntu runner**，以使用公开仓库的免费运行方式；个人设备密钥仍放在加密的 Actions Secrets。若选择私有仓库，需自行核实账号额度和预算，不能默认永远免费。

把本目录里的文件放在仓库根目录，保留 `.github/workflows/monitor.yml`。GitHub 网页上传时通常不容易上传隐藏目录，可以先上传其他文件，再点 **Add file → Create new file**，文件名填 `.github/workflows/monitor.yml`，粘贴同名文件的内容并提交。不要只把整个 ZIP 文件上传到仓库。

在仓库中打开 **Settings → Secrets and variables → Actions → New repository secret**：

| Name | Secret |
|---|---|
| `BARK_KEY` | Bark 首页复制的 `https://api.day.app/…` 个人推送链接，或其中的设备密钥 |

默认使用 Bark 的 `https://api.day.app` 服务。服务器会收到用于发到你的手机的设备密钥和通知内容；不需要 DMIT 密码。程序和日志不会打印密钥。

## 第三步：必须完成两项实测

在 **Actions → DMIT Premium Monitor → Run workflow** 中选择：

1. **test**：发送明确标为“测试”的 30 秒重要警告。将 iPhone 锁屏，并分别测试静音和专注模式，确认能听见。
2. **check**：从 GitHub 云端实际访问 DMIT，读取库存与月付、年付选项；不会发补货提醒。打开运行日志，必须看到成功记录。
3. 上述两项都通过后，选 **scan** 手动跑一次。随后每小时第 7、37 分钟的定时任务会按相同条件检查。

定时配置位于默认分支才会生效。没有设置 `BARK_KEY` 时，定时检查会明确失败，不会假装已接通提醒。

**如果云端 check 返回 403、验证码或超时：当前部署不能判断库存，不能宣称监控已启用。** 本程序不会绕过验证码；需改用能正常访问 DMIT 的运行环境或合适的监控服务。你本机浏览器能打开，不代表 GitHub 的机房 IP 也能打开。

## 长期运行需要知道

- GitHub 官方说明定时任务可能延迟，负载极高时可能丢弃排队任务；本方案不能保证严格每 30 分钟准点，也可能错过两次检查之间短暂的补货。
- 公开仓库 **60 天没有仓库活动可能自动停用定时工作流**；需要留意 Actions 状态，按 GitHub 页面提示重新启用。仅仅定时运行不等于仓库有更新。手机日历每月提醒检查一次也有帮助。
- 去重状态保存在 GitHub Actions Cache。缓存可能被清理；清理后如果仍有货，可能重新提醒一次。缓存不包含 Bark 密钥。不要把缓存当成永久数据库。
- 检查失败会保留上一次已知商品状态，不把“打不开网页”当成缺货或补货。
- 无货且正常时不会给手机发周期性消息。GitHub 本身可能有工作流失败邮件，可在 GitHub 的通知设置中管理。
- 如果工作流根本没有启动，程序也无法发故障通知；因此不能用“没收到通知”推断监控正常。
- 库存和线路可能调整，Premium 是按 DMIT 官方线路类别筛选，不是对你所在地三家运营商做实时路由测量。下单前仍以官方商品及结账信息为准。
- 停止：Actions → 选中工作流 → 右上角菜单 → Disable workflow。

## 本地验证（可选）

```sh
python -m unittest -v
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python monitor.py --mode check
```

Linux 无桌面环境时，最后一条命令前加 `xvfb-run --auto-servernum`；或设 `HEADLESS=1`，但不同浏览器模式可能被网站区别对待。

`test_monitor.py` 检查严格价格边界、Premium 范围、真实年费、缺货与未知状态、去重、推送失败重试和连续故障通知。

## 来源

- [DMIT 官方网络分类](https://www.dmit.io/)
- [DMIT 实际购物车](https://www.dmit.io/cart.php)
- [Bark 官方 API](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)
- [GitHub 定时任务限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

初次人工核实：2026-10-31（北京时间）。当时 Premium TINY（$10.90/月）、Pocket（$16.90/月）均显示缺货。这是当时结果，不代表之后的实时库存。
