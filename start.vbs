Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Try py (Python launcher) then python
If fso.FileExists("C:\Windows\py.exe") Then
    cmd = "py"
Else
    cmd = "python"
End If

shell.Run "cmd /c cd /d """ & scriptDir & """ && " & cmd & " app.py", 0, False
