# 逆光365 v2 启动项

本目录是当前生产机的登录自启实现，详细状态见 `docs/DEPLOYMENT.md` 第 7 节。

| 文件 | 用途 |
|---|---|
| `start-web.ps1` | 检查 8501 健康；未启动时隐藏启动 v2 Web，日志写入 `D:\逆光365图片下载\.niguang365\logs` |
| `start-chrome.ps1` | 检查 9222 CDP；未启动时用独立 profile 启动项目 Chrome |
| `start-stack.vbs` | 依次隐藏调用上面两个脚本；由 HKCU Run 的 `Niguang365Stack` 值调用 |
| `register-autostart.ps1` | 有管理员权限的机器可改用计划任务 `Niguang365-Stack`；当前机器因 `Access is denied` 未使用 |

当前机器实际自启项：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
  Niguang365Stack = wscript.exe "D:\Code\VSCode\BKLT365\deploy\start-stack.vbs"
```

注意：两个 `.ps1` 含中文路径，必须保存为 **UTF-8 with BOM**，否则 Windows PowerShell 5.1 会读错路径。
