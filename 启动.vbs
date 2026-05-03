Dim WshShell, strPath, pythonPath
Set WshShell = CreateObject("WScript.Shell")
strPath = WshShell.CurrentDirectory & "\"
pythonPath = strPath & "venv\Scripts\python.exe"

If Not CreateObject("Scripting.FileSystemObject").FileExists(pythonPath) Then
    MsgBox "未找到虚拟环境，请先运行 python setup.py", 48, "Video-to-Doc"
    WScript.Quit 1
End If

WshShell.Run """" & pythonPath & """ """ & strPath & "gui.py""", 0, False
