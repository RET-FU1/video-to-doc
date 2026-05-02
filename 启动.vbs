Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = dir

pythonw = dir & "\venv\Scripts\pythonw.exe"
gui = dir & "\gui.py"

If Not fso.FileExists(pythonw) Then
    MsgBox "未找到虚拟环境，请先运行: python setup.py", 48, "Video-to-Doc"
    WScript.Quit 1
End If

On Error Resume Next
ws.Run """" & pythonw & """ """ & gui & """", 0, False

If Err.Number <> 0 Then
    MsgBox "启动失败: " & Err.Description, 16, "Video-to-Doc"
End If
