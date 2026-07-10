Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Auto-update from GitHub if Git is installed
If fso.FileExists("C:\Program Files\Git\bin\git.exe") Or fso.FileExists("C:\Program Files (x86)\Git\bin\git.exe") Then
    shell.Run "cmd /c cd /d """ & scriptDir & """ && git pull", 0, True
End If

' Try py (Python launcher) then python
If fso.FileExists("C:\Windows\py.exe") Then
    cmd = "py"
Else
    cmd = "python"
End If

shell.Run "cmd /c cd /d """ & scriptDir & """ && " & cmd & " app.py", 0, False
