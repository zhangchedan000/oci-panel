# OCI 控制面板（完整版 v1.0）

Y 探长的**安全复刻**：多账号集中管理，每个账号是密封单元（独立凭证 + 独立出口 IP + 独立指纹 + 独立限流）；全手动、无自动重试、无跨账号批量、无后台抢机。

## 功能

**实例**：列表/状态灯/公网IP、开机·关机·重启、换公网IP（手动+冷却+二次确认）、改配置(弹性核数/内存)、克隆(新建)、重建(删除+重launch)、删除、附加IPv6
**存储**：引导卷查看 + 扩容
**网络**：防火墙(安全列表入站规则 查看/添加/删除)
**只读**：配额查询、流量统计(近6小时)
**账号**：多账号切换；**在面板里添加/删除账号**（填 OCID+粘贴私钥+选出口IP，自动写 keys/ 和 accounts.yaml 并热加载；非 HTTPS 会警告勿贴私钥）；每账号「出口 IP 设置」窗口(代理/源IP/直连，可热切换)+「检测当前出口 IP」核对绑定；每账号固定指纹(自动分配)

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

一条命令装好，**其余全部在网页里完成，不用再碰配置文件**：

1. 跑上面命令 → 自动克隆、生成初始配置、装好服务并启动
2. 终端打印**控制台地址**和一个**首次设置令牌**
3. 浏览器打开地址 → 输令牌 + 设登录用户名/密码（首次）
4. 登录后点**「添加账号」**逐个加 OCI 账号（填 OCID + 粘贴私钥 + 选出口 IP）
5. 放行端口：云安全组 + `sudo ufw allow 9000`

之后改出口 IP、增删账号、改密码，全在面板窗口里点，`accounts.yaml` 基本不用手动编辑。

> 在网页里粘贴私钥前，最好已经配好 HTTPS（NPM/Caddy）或走 SSH 隧道——非 HTTPS 时添加表单会红字警告。

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
