' Niguang365-Stack startup launcher.
' Registered under HKCU Run; starts the v2 Web service and the isolated project Chrome/CDP profile.
' Remove the Niguang365Stack Run value to disable both.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\Code\VSCode\BKLT365\deploy\start-web.ps1""", 0, False
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\Code\VSCode\BKLT365\deploy\start-chrome.ps1""", 0, False
