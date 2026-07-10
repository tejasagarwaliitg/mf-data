Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

shell.Run "cmd /c cd /d """ & scriptDir & """ && pip install -q -r requirements.txt 2>nul && python app.py", 0, False

WScript.Sleep 3000
shell.Run "http://localhost:5050"
