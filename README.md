# OCI 控制面板（完整版 v1.0）

Y 探长的**安全复刻**：多账号集中管理，每个账号是密封单元（独立凭证 + 独立出口 IP + 独立指纹 + 独立限流）；全手动、无自动重试、无跨账号批量、无后台抢机。

## 功能

**实例**：列表/状态灯/公网IP、开机·关机·重启、换公网IP（手动+冷却+二次确认）、改配置(弹性核数/内存)、克隆(新建)、重建(删除+重launch)、删除、附加IPv6
**存储**：引导卷查看 + 扩容
**网络**：防火墙(安全列表入站规则 查看/添加/删除)
**只读**：配额查询、流量统计(近6小时)
**账号**：多账号切换；每账号「出口 IP 设置」窗口(代理/源IP/直连，可热切换)+「检测当前出口 IP」核对绑定；每账号固定指纹(自动分配)

## 防关联（代码里已落实）

- **出口隔离**：每账号 OCI 客户端注入独立 `requests.Session`，走各自代理或绑定源 IP。
- **指纹稳定**：每账号一个固定、逼真的 UA(`identity.py`/`identity.json`)，生成一次不变，只有换 API 密钥才换。
- **无自动重试**：所有客户端 `NoneRetryStrategy`；抢机降级为「克隆/重建」单发，容量不足即失败、不循环。
- **无跨账号入口**：每个操作必须指定单一账号。
- **硬限流**：每账号 `min_interval + 抖动` + 每小时上限。

> IP 隔离解决 IP 层关联；**支付方式仍是最硬的关联信号**（注册环节，面板管不了）。

## 密钥安全

- OCI API Key **本地签名、私钥不上网**。
- `.gitignore` 挡 `keys/ *.pem accounts.yaml identity.json settings.json`。
- `install.sh` 自动 `chmod 700 keys/ + 600 *.pem/identity.json/settings.json`；**启动时密钥权限过松则拒绝启动**；关闭 `/docs`。

## 部署

### 一键安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/zhangchedan000/oci-panel/main/bootstrap.sh)
```

首次运行会自动克隆代码并生成 `~/oci-panel/accounts.yaml`，然后退出（登录密码不在这里设）。
编辑它（面板密码 + 各账号 API Key + 各自出口 IP；私钥放 `~/oci-panel/keys/`），
再**重跑上面这条命令**即可装好 systemd 服务。

默认绑定 `0.0.0.0:9000`（公网可访问）；安装结束会**打印控制台地址**和一个**首次设置令牌**。

**首次设置密码（在网页上）**：打开控制台地址 → 输入终端显示的**设置令牌** + 你要设的用户名/密码 → 完成。
令牌用后即失效。之后登录，可在面板右上角**「改密码」**随时修改。
> 令牌机制是为了防止面板绑公网后、设密码前被人抢先占用。
需放行端口：云安全组 + 本机 `sudo ufw allow 9000`。
只想绑本机走反代/隧道： `HOST=127.0.0.1 bash <(curl ...bootstrap.sh)`。
面板握有云密钥，请设强密码，并尽快用 NPM/Caddy 加 HTTPS。

### 一键卸载

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/zhangchedan000/oci-panel/main/uninstall.sh)
```

停止并移除服务；会询问是否连同目录（含密钥）一起删除。
彻底删除且不询问： `PURGE=1 bash <(curl -fsSL .../uninstall.sh)`

### 手动安装（等价）

```bash
git clone https://github.com/zhangchedan000/oci-panel.git && cd oci-panel
bash install.sh          # 首次生成 accounts.yaml 后退出
bash install.sh          # 编辑好 accounts.yaml 后再跑，装服务
```

**务必** Nginx/Caddy 反代 + HTTPS，不要裸 HTTP 暴露。日志 `journalctl -u oci-panel -f`。

## 说明

- 出口 IP 之后随时在**面板窗口里改**，无需动配置文件；`accounts.yaml` 的 egress 只是初始默认。
- 换 IP 只对**临时(Ephemeral)公网 IP** 生效（免费实例默认就是）。
- 「重建」会先删除当前实例再尝试创建新的（单次尝试）；容量不足会失败、需手动再点，**不会自动重试**。
- **未包含**（可后续加）：Cloudflare DNS、面板 MFA、TG 机器人、Cloud Shell（与"不登录控制台"原则冲突，故不做）。
- 面板逻辑与隔离层已在本地充分测试；**具体 OCI 调用需你在真实账号上跑一遍验证**（沙箱无法连真实 OCI）。
